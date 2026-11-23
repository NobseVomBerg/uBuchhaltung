# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""E-Rechnung und PDF für Positionen mit der Momentaufnahme eines Steuersatzes aus den Stammdaten (2.8.0, Etappe 4:
``export/einvoice_model.py``, ``export/pdf_document.py``, ``export/pdf_invoice.py``, ``export/pdf_quote.py``).

Eine Position mit Momentaufnahme trägt ihren Satz in Hundertstel Prozent (``InvoiceItems.TaxRateBp``): Er geht ihrem ``TaxRate`` vor, ihre Gruppe rechnet die
Steuer einstufig (EN 16931, BR-CO-17), und ein steuerfreier Eintrag steht als Art ``'E'`` mit seinem Befreiungsgrund (``TaxExemptReason``). Modell, CII, UBL
und PDF lesen dieselben Gruppen – auf den Cent die gespeicherten Summen. Der Befreiungsgrund ist BT-120 (CII ``ram:ExemptionReason``, UBL
``cbc:TaxExemptionReason``) und steht unter dem Summenblock des PDF – nie der Hinweis auf § 19 UStG, der allein dem Beleg ohne Umsatzsteuerausweis gehört.
Einen Befreiungscode (BT-121) führt die App nicht. Fehlt einer steuerfreien Gruppe der Grund, entsteht kein Text, dafür eine Lücke der Prüfung
(``einvoice_gaps``: „Grund der Steuerbefreiung“) – das UBL wird dann nicht geschrieben.

Bis die Maske ab Etappe 3 solche Positionen schreibt, legen diese Tests die Belege direkt an (``insert_invoice``/``insert_invoice_item`` mit den Schlüsseln der
Momentaufnahme, die Summen aus den Positionen mit ``tax_groups.item_groups`` gerechnet – wie das Speichern sie ablegen wird). Ein Beleg OHNE Momentaufnahme gibt
Byte für Byte dieselben Dokumente aus wie vorher; das halten die gepinnten Hashes und Golden-Dateien der übrigen Tests fest (``tests/test_single_rate_snapshots.py``,
``tests/test_mixed_tax_documents.py``, ``tests/test_golden_invoice_outputs.py``) – hier steht, was die Momentaufnahme ändert, und die Gegenprobe, dass sie
ohne Momentaufnahme nichts ändert.

Nacharbeiten an der Etappe (Prüfung der Dokumente): Der Befreiungsgrund kommt aus EINER Funktion (``exempt_reason``) in E-Rechnung und PDF – der erste nicht leere Grund der
Positionen mit steuerfreier Momentaufnahme, bereinigt und auf ``MAX_REASON_LENGTH`` Zeichen gekürzt –, der Hinweis im PDF wird gemessen, wie er gedruckt wird (WinAnsi-Ersetzungen,
Breiten der eingebetteten Schrift), ``item_bp`` liest streng (0 und 1 bis 100 Prozent), die Steuerzeile eines Belegs mit EINEM Satz und Momentaufnahme setzt den Satz mit Dezimalkomma,
und zwei Befunde (Momentaufnahme weicht vom ``TaxRate`` ab, steuerfreie Position mit unlesbarer Momentaufnahme) stehen mit einer Zeile im Server-Log.

Alle Namen, Adressen, Gründe und Beträge sind erfundene Testwerte.
"""
import contextlib
import io
import random
import re
import sqlite3
import xml.etree.ElementTree as ET
from decimal import ROUND_HALF_UP, Decimal

import pdfplumber
import pytest

import userctx
from export import pdf_document as D
from export.cii_invoice import build_cii
from export.einvoice_model import (DIFFERENT_REASONS_MESSAGE, EXEMPT_REASON_KLEINUNTERNEHMER, SNAPSHOT_FINDING_MESSAGE, ExemptReason, Gap, TaxBreakdown,
                                   build_einvoice, einvoice_gaps, exempt_reason, exemption_note, item_bp, log_snapshot_findings, log_unflagged_rates,
                                   per_item_rates, snapshot_rate_text)
from export.invoice_files import generate_invoice_documents
from export.pdf_invoice import generate_invoice_pdf
from export.pdf_quote import generate_quote_pdf
from export.pdf_worktime import info_width, printed_text
from export.xrechnung_invoice import XRechnungGenerator
from money import format_de, from_minor, to_minor
from tax_groups import MAX_REASON_LENGTH, bp_pct, group_tax, item_groups, mixed_groups
from tests.invoice_world import build_world, columns, query_rows, run_sql
from tests.single_rate_helpers import embedded_xml

USER = 'anna'
REASON = 'Steuerfreie Beispielleistung nach dem Testgesetz (erfundener Grund)'
OTHER_REASON = 'Steuerfreie Beispielleistung nach dem zweiten Testgesetz (ebenfalls erfunden)'
HEADER = 'Pos. Menge Einheit Bezeichnung USt Einzelpreis Gesamt'
HEADER_SINGLE_RATE = 'Pos. Menge Einheit Bezeichnung Einzelpreis Gesamt'
PARA_19_NOTE = EXEMPT_REASON_KLEINUNTERNEHMER


@pytest.fixture
def world(tmp_db, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    userctx.set_user(USER)
    return build_world(tmp_db)


# ── Belege bauen ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _position(number, description, price, quantity, **stored):
    return {'position': number, 'description': description, 'quantity': quantity, 'unit': 'C62', 'price_per_unit': Decimal(price),
            'total_net': Decimal(price) * Decimal(str(quantity)), **stored}


def plain(number, description, price, rate, quantity=1, category='S'):
    """Eine Position OHNE Momentaufnahme, wie bisher: Der Satz steht allein in ``TaxRate`` (Prozentzahl wie in der Maske)."""
    return _position(number, description, price, quantity, tax_category=category, tax_rate=rate)


def standard(number, description, price, bp, quantity=1):
    """Eine Position mit der Momentaufnahme eines Standardsatzes: ``bp`` in Hundertstel Prozent (1900, 550; 0 ist die Kategorie Z)."""
    return _position(number, description, price, quantity, tax_category='S', tax_rate=float(bp_pct(bp)), tax_rate_bp=bp)


def exempt(number, description, price, reason=REASON, quantity=1):
    """Eine Position mit der Momentaufnahme eines steuerfreien Eintrags: Art ``E``, Satz 0 und der Grund (``None``: ohne Grund)."""
    return _position(number, description, price, quantity, tax_category='E', tax_rate=0, tax_rate_bp=0, tax_exempt_reason=reason)


def snapshot_over_rate(number, description, price, bp, rate, quantity=1):
    """Eine Position, deren ``TaxRate`` vom Satz der Momentaufnahme abweicht: ``bp`` in Hundertstel Prozent, ``rate`` die Prozentzahl in ``TaxRate``. Die App schreibt so etwas nicht
    (``standard`` setzt beides gleich), die SQL-Konsole schon – dann gilt die Momentaufnahme."""
    return _position(number, description, price, quantity, tax_category='S', tax_rate=rate, tax_rate_bp=bp)


def make_document(world, positions, number, *, head_rate=None, mixed=None, quote=False, **over):
    """Ein Beleg, direkt angelegt: der Kopf (alles, was die E-Rechnung verlangt, damit ihr nichts fehlt), die Positionen samt Momentaufnahme und die Summen –
    mit ``tax_groups.item_groups`` aus den Positionen gerechnet, wie das Speichern sie ablegt. ``head_rate``: der Satz im Kopf als Bruch (Standard: der höchste
    Satz der Positionen; −1 ist der Platzhalter für „ohne Steuerausweis“), ``mixed``: das Kennzeichen „Steuersatz je Position“ (``None`` oder 1)."""
    groups = item_groups([(row['tax_rate'], to_minor(row['total_net']), row.get('tax_rate_bp')) for row in positions])
    net, tax = sum(group.net for group in groups), sum(group.tax for group in groups)
    if head_rate is None:
        head_rate = float(max(group.pct for group in groups) / 100)
    data = {
        'invoice_number': number, 'invoice_date': '2026-06-07', 'delivery_date': '2026-06-05', 'own_company_id': world.own,
        'seller_name': 'Anna Beispiel', 'seller_company': 'Eigene Firma GmbH', 'seller_street': 'Hauptstr. 1', 'seller_postal_code': '10115',
        'seller_city': 'Berlin', 'seller_country': 'DE', 'seller_vat_id': 'DE123456789', 'seller_email': 'info@example.com',
        'seller_phone': '030 1234567', 'customer_id': world.cust, 'buyer_name': 'Max Muster', 'buyer_company': 'Malermeister Pinsel',
        'buyer_street': 'Kundenweg 5', 'buyer_postal_code': '54321', 'buyer_city': 'Kundenhausen', 'buyer_country': 'DE',
        'buyer_route_id': '04011000-1234512345-35', 'payment_terms': 'Zahlbar in 14 Tagen', 'payment_due_date': '2026-06-21',
        'bank_account_id': world.bank, 'bank_name': 'Testbank AG', 'bank_iban': 'DE89370400440532013000', 'bank_bic': 'TESTDE1X',
        'tax_category': 'S', 'tax_rate': head_rate, 'sum_net': from_minor(net), 'tax_amount': from_minor(tax), 'sum_gross': from_minor(net + tax),
        'amount_due': from_minor(net + tax), 'status': 'finalized', 'mixed_tax_rates': mixed,
    }
    if quote:
        data.update(document_type='quote', status='draft', valid_until='2026-07-07', intro_text='Guten Tag',
                    closing_text='Mit freundlichen Grüßen', buyer_route_id=None)
    data.update(over)
    document_id = world.db.insert_invoice(data)
    for row in positions:
        world.db.insert_invoice_item({'invoice_id': document_id, **row})
    return document_id


def _insert_row(db, table, row):
    """Eine Zeile als Kopie einfügen (die Spaltennamen stammen aus der Tabelle, die Werte gehen als Parameter)."""
    row = {key: value for key, value in row.items() if key != 'ID'}
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        cursor = con.execute(f'INSERT INTO {table} ({", ".join(row)}) VALUES ({", ".join("?" * len(row))})', list(row.values()))
        con.commit()
        return cursor.lastrowid


def credit_note(world, original_id, number='26S300'):
    """Die Stornorechnung zu ``original_id``, so, wie ``cancel_invoice`` sie schreibt: Kopf kopiert, Summen exakt negiert, Positionen mit negativer Menge und
    positivem Einzelpreis – samt Momentaufnahme (jede Spalte der Position wird kopiert). Von Hand gebaut: Diese Tests prüfen nur das Dokument, nicht die Buchungen."""
    stored = columns(world.db, original_id)
    head = {**stored, 'InvoiceNumber': number, 'InvoiceKind': 'storno', 'CancelsInvoiceId': original_id, 'Status': 'sent',
            'SumNet': -stored['SumNet'], 'TaxAmount': -stored['TaxAmount'], 'SumGross': -stored['SumGross'], 'AmountDue': -stored['SumGross'],
            'PaymentTerms': 'Der Betrag wird verrechnet bzw. erstattet.', 'PaymentDueDate': None, 'SkontoDays': None, 'SkontoPercent': None,
            'PDFPath': None, 'XMLPath': None, 'DocumentVersion': None}
    new_id = _insert_row(world.db, 'Invoices', head)
    for position in query_rows(world.db, 'SELECT * FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', original_id):
        _insert_row(world.db, 'InvoiceItems', {**position, 'InvoiceId': new_id, 'Quantity': -position['Quantity'], 'TotalNet': -position['TotalNet']})
    return new_id


def model_of(world, document_id):
    return build_einvoice(world.db, world.db.get_invoice_by_id(document_id), world.db.get_invoice_items(document_id))


def cii_of(world, document_id):
    return build_cii(model_of(world, document_id))


def ubl_of(world, document_id):
    return XRechnungGenerator().generate_from_model(model_of(world, document_id)).encode('utf-8')


def invoice_pdf(world, document_id):
    pdf, _path = generate_invoice_pdf(world.db, document_id)
    assert pdf
    return pdf


def quote_pdf(world, document_id):
    pdf, _path = generate_quote_pdf(world.db, document_id)
    assert pdf
    return pdf


# ── Was in den Dokumenten steht ──────────────────────────────────────────────────────────────────────────────────────────────────

def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def _children(element):
    """Die Kinder in Dokumentreihenfolge als ``(Name, Text)`` – der Text eines Blatts, ``None`` bei einem Element mit Kindern."""
    return [(_local(child), (child.text or '').strip() if len(child) == 0 else None) for child in element]


def cii_taxes(xml):
    """Die Steuer einer CII: ``(Aufschlüsselung, je Position)`` – je ``ram:ApplicableTradeTax`` die Kinder in Dokumentreihenfolge."""
    root = ET.fromstring(xml)
    settlement = next(el for el in root.iter() if _local(el) == 'ApplicableHeaderTradeSettlement')
    header = [_children(el) for el in settlement if _local(el) == 'ApplicableTradeTax']
    lines = [_children(el) for line in root.iter() if _local(line) == 'IncludedSupplyChainTradeLineItem'
             for el in line.iter() if _local(el) == 'ApplicableTradeTax']
    return header, lines


def ubl_taxes(xml):
    """Die Steuer eines UBL: ``(je TaxSubtotal (Basis, Steuer, Kinder von TaxCategory), je Position die Kinder von ClassifiedTaxCategory)``."""
    root = ET.fromstring(xml)
    subtotals = []
    for subtotal in (el for el in root.iter() if _local(el) == 'TaxSubtotal'):
        leaves = dict(_children(subtotal))
        category = next(el for el in subtotal if _local(el) == 'TaxCategory')
        subtotals.append((leaves['TaxableAmount'], leaves['TaxAmount'], _children(category)))
    lines = [_children(el) for el in root.iter() if _local(el) == 'ClassifiedTaxCategory']
    return subtotals, lines


def skeleton(xml):
    """Der Aufbau eines Dokuments ohne Texte: je Element (Tiefe, Name) in Dokumentreihenfolge."""
    result = []

    def walk(element, depth):
        result.append((depth, _local(element)))
        for child in element:
            walk(child, depth + 1)

    walk(ET.fromstring(xml), 0)
    return result


def page_rows(pdf, page=0):
    """Die Zeilen einer Seite von oben nach unten: ``(Text, Wörter)`` – die Wörter einer Zeile von links nach rechts (``pdfplumber``: ``x0``, ``x1``, ``top``,
    ``bottom``). Wörter mit ähnlicher Höhe (bis 3 pt) gehören zu einer Zeile."""
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        words = sorted(document.pages[page].extract_words(x_tolerance=3), key=lambda w: (w['top'], w['x0']))
    rows = []
    for word in words:
        if rows and abs(word['top'] - rows[-1][0]) <= 3:
            rows[-1][1].append(word)
        else:
            rows.append([word['top'], [word]])
    return [(' '.join(w['text'] for w in sorted(row, key=lambda w: w['x0'])), sorted(row, key=lambda w: w['x0'])) for _top, row in rows]


def page_lines(pdf, page=0):
    return [text for text, _words in page_rows(pdf, page)]


def page_count(pdf):
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        return len(document.pages)


def lines_after(lines, start_with, count):
    """Die ``count`` Zeilen ab der ersten, die mit ``start_with`` beginnt."""
    start = next(index for index, line in enumerate(lines) if line.startswith(start_with))
    return lines[start:start + count]


def note_rows(pdf, page=0):
    """Die Zeilen unter dem Summenblock, bis zum Zahlungshinweis: ``(Text, Wörter)``."""
    rows = page_rows(pdf, page)
    start = next(index for index, (text, _words) in enumerate(rows) if text.startswith('Gesamtbetrag:')) + 1
    after = ('Zahlbar in', 'Der Betrag wird verrechnet', 'Mit freundlichen')            # der Zahlungshinweis (Rechnung, Stornorechnung) und der Schluss des Angebots
    end = next((index for index, (text, _words) in enumerate(rows) if index >= start and text.startswith(after)), len(rows))
    return rows[start:end]


# ── Ganz steuerfrei: Kategorie E, Satz 0 und der Grund der Momentaufnahme ─────────────────────────────────────────────────────────

def test_a_fully_exempt_document_is_category_e_with_rate_0_and_the_reason_of_the_snapshot_in_the_model(world):
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '50.00', quantity=2), exempt(2, 'Lernmaterial', '30.00')], '26R301')

    model = model_of(world, document)

    assert [(line.tax_category, line.tax_rate, line.exact) for line in model.lines] == [('E', Decimal(0), True)] * 2
    assert model.tax_breakdown == [TaxBreakdown('E', Decimal(0), to_minor('130.00'), 0, REASON)]
    assert model.is_exempt and model.warnings == [] and einvoice_gaps(model) == []
    assert (model.totals.line_total, model.totals.tax_total, model.totals.grand_total) == (to_minor('130.00'), 0, to_minor('130.00'))


def test_a_fully_exempt_document_carries_the_reason_as_bt_120_in_cii_and_ubl_and_never_the_text_of_para_19(world):
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R302')

    cii, ubl = cii_of(world, document), ubl_of(world, document)

    header, lines = cii_taxes(cii)
    assert header == [[('CalculatedAmount', '0.00'), ('TypeCode', 'VAT'), ('ExemptionReason', REASON), ('BasisAmount', '80.00'),
                       ('CategoryCode', 'E'), ('RateApplicablePercent', '0.00')]]
    assert lines == [[('TypeCode', 'VAT'), ('CategoryCode', 'E'), ('RateApplicablePercent', '0.00')]]            # an der Position kein Grund (EN 16931)
    subtotals, line_categories = ubl_taxes(ubl)
    assert subtotals == [('80.00', '0.00', [('ID', 'E'), ('Percent', '0'), ('TaxExemptionReason', REASON), ('TaxScheme', None)])]
    assert line_categories == [[('ID', 'E'), ('Percent', '0'), ('TaxScheme', None)]]
    for xml in (cii, ubl):
        assert '§ 19' not in xml.decode() and 'Kleinunternehmer' not in xml.decode()
        assert b'ExemptionReasonCode' not in xml and b'TaxExemptionReasonCode' not in xml                       # einen Befreiungscode führt die App nicht


def test_a_document_after_para_19_keeps_the_text_of_para_19_in_the_model_cii_and_ubl(world):
    """Der Beleg ohne Umsatzsteuerausweis (Kopf −1) trägt den Hinweis auf § 19 UStG wie bisher – auch ein Werkzeug, das ``'E'`` ohne Momentaufnahme an seine
    Positionen schreibt (``tools/make_einvoice_samples.py``), ändert daran nichts."""
    document = make_document(world, [plain(1, 'Beratung', '100.00', 0, category='E')], '26R303', head_rate=-1)

    model = model_of(world, document)

    assert model.tax_breakdown == [TaxBreakdown('E', Decimal(0), to_minor('100.00'), 0, PARA_19_NOTE)]
    assert [line.exact for line in model.lines] == [False]
    assert einvoice_gaps(model) == []
    assert cii_taxes(cii_of(world, document))[0][0][2] == ('ExemptionReason', PARA_19_NOTE)
    assert ubl_taxes(ubl_of(world, document))[0][0][2][2] == ('TaxExemptionReason', PARA_19_NOTE)


def test_the_head_after_para_19_wins_even_if_the_positions_carry_a_snapshot(world):
    """Ohne Umsatzsteuerausweis gibt es keine Momentaufnahme (die App lehnt sie ab); steht trotzdem eine da (SQL-Konsole), gilt der Kopf: Hinweis auf § 19."""
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R304', head_rate=-1)

    assert model_of(world, document).tax_breakdown[0].exemption_reason == PARA_19_NOTE
    lines = page_lines(invoice_pdf(world, document))
    assert any(line.startswith('Es gilt die Steuerbefreiung für Kleinunternehmer nach § 19 UStG') for line in lines)
    assert not any(REASON in line for line in lines)


def test_an_exempt_row_without_a_snapshot_in_a_document_with_tax_is_read_as_before(world):
    """Ohne Momentaufnahme bleibt jede Gruppe, wie sie war – auch eine Position der Art ``'E'`` in einem Beleg mit Steuerausweis (Bestand, Import, SQL-Konsole)."""
    document = make_document(world, [plain(1, 'Beratung', '100.00', 0, category='E')], '26R305', head_rate=0)

    model = model_of(world, document)

    assert model.tax_breakdown == [TaxBreakdown('E', Decimal(0), to_minor('100.00'), 0, PARA_19_NOTE)]
    assert exemption_note(world.db.get_invoice_items(document)) is None


# ── 19 % und steuerfrei: zwei Aufschlüsselungen, Steuer und Summen wie gespeichert ───────────────────────────────────────────────────

def mixed_with_exempt(world, number='26R310', reason=REASON, **over):
    """100,00 € zu 19 % (eine Position ohne Momentaufnahme: Der Payload von heute trägt die Sätze der Vorbelegung ohne sie) und 80,00 € steuerfrei (mit
    Momentaufnahme): netto 180,00, Steuer 19,00, brutto 199,00."""
    return make_document(world, [plain(1, 'Beratung', '100.00', 19), exempt(2, 'Nachhilfestunde', '80.00', reason)], number, mixed=1, **over)


def test_19_percent_and_exempt_give_two_breakdowns_e_and_s_with_the_stored_sums(world):
    document = mixed_with_exempt(world)
    stored = columns(world.db, document)

    model = model_of(world, document)

    assert model.tax_breakdown == [TaxBreakdown('E', Decimal(0), to_minor('80.00'), 0, REASON),
                                   TaxBreakdown('S', Decimal(19), to_minor('100.00'), to_minor('19.00'), '')]
    assert (model.totals.line_total, model.totals.tax_total, model.totals.grand_total) == (stored['SumNet'], stored['TaxAmount'], stored['SumGross'])
    assert model.warnings == [] and einvoice_gaps(model) == []
    assert [(line.tax_category, line.exact) for line in model.lines] == [('S', False), ('E', True)]           # nur die Position mit Momentaufnahme rechnet einstufig


def test_cii_and_ubl_of_19_percent_and_exempt_name_the_reason_only_in_the_exempt_breakdown(world):
    document = mixed_with_exempt(world)

    header, lines = cii_taxes(cii_of(world, document))
    subtotals, line_categories = ubl_taxes(ubl_of(world, document))

    assert header == [
        [('CalculatedAmount', '0.00'), ('TypeCode', 'VAT'), ('ExemptionReason', REASON), ('BasisAmount', '80.00'), ('CategoryCode', 'E'),
         ('RateApplicablePercent', '0.00')],
        [('CalculatedAmount', '19.00'), ('TypeCode', 'VAT'), ('BasisAmount', '100.00'), ('CategoryCode', 'S'), ('RateApplicablePercent', '19.00')]]
    assert [dict(line)['CategoryCode'] for line in lines] == ['S', 'E']
    assert subtotals == [('80.00', '0.00', [('ID', 'E'), ('Percent', '0'), ('TaxExemptionReason', REASON), ('TaxScheme', None)]),
                         ('100.00', '19.00', [('ID', 'S'), ('Percent', '19'), ('TaxScheme', None)])]
    assert [dict(line)['ID'] for line in line_categories] == ['S', 'E']


def test_the_order_of_the_children_is_the_one_of_a_document_after_para_19_only_the_text_of_the_reason_differs(world):
    """CII: ``ApplicableTradeTax`` (CalculatedAmount, TypeCode, ExemptionReason, BasisAmount, CategoryCode, RateApplicablePercent), UBL: ``TaxCategory`` (ID, Percent,
    TaxExemptionReason, TaxScheme) – wie beim Beleg nach § 19; der Aufbau beider Dokumente ist derselbe, nur der Text des Grundes ist ein anderer."""
    snapshot = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R311')
    para_19 = make_document(world, [plain(1, 'Nachhilfestunde', '80.00', 0, category='E')], '26R312', head_rate=-1)

    assert skeleton(cii_of(world, snapshot)) == skeleton(cii_of(world, para_19))
    assert skeleton(ubl_of(world, snapshot)) == skeleton(ubl_of(world, para_19))
    assert [name for name, _text in cii_taxes(cii_of(world, snapshot))[0][0]] == [name for name, _text in cii_taxes(cii_of(world, para_19))[0][0]]
    assert [name for name, _text in ubl_taxes(ubl_of(world, snapshot))[0][0][2]] == [name for name, _text in ubl_taxes(ubl_of(world, para_19))[0][0][2]]


# ── 19 % und 5,5 %: einstufig gerundet, in XML und PDF wie die gespeicherte Summe ────────────────────────────────────────────────────

def mixed_with_5_5(world, number='26R320', **over):
    """100,00 € zu 19 % (ohne Momentaufnahme, Steuer 19,00) und 0,09 € zu 5,5 % (mit Momentaufnahme): einstufig 0,00 (0,00495), zweistufig 0,01 – netto 100,09,
    Steuer 19,00, brutto 119,09."""
    return make_document(world, [plain(1, 'Beratung', '100.00', 19), standard(2, 'Kleinteil', '0.09', 550)], number, mixed=1, **over)


def test_the_two_rounding_rules_differ_for_0_09_euro_at_5_5_percent():
    net, rate = to_minor('0.09'), Decimal('5.5')

    assert group_tax(net, rate) == to_minor('0.01')                       # zweistufig, wie vor 2.8.0
    assert group_tax(net, rate, exact=True) == 0                          # einstufig nach BR-CO-17: 0,09 € × 5,5 % = 0,00495 € = 0,00 €


def test_the_tax_of_the_5_5_percent_group_is_0_00_in_model_cii_and_ubl_and_equals_the_stored_sum(world):
    document = mixed_with_5_5(world)
    stored = columns(world.db, document)
    assert stored['TaxAmount'] == to_minor('19.00') and stored['SumGross'] == to_minor('119.09')

    model = model_of(world, document)

    assert [(t.category, t.rate, t.basis, t.amount) for t in model.tax_breakdown] == [
        ('S', Decimal('5.5'), to_minor('0.09'), 0), ('S', Decimal(19), to_minor('100.00'), to_minor('19.00'))]
    assert (model.totals.tax_total, model.totals.grand_total) == (stored['TaxAmount'], stored['SumGross']) and model.warnings == []
    header, _lines = cii_taxes(cii_of(world, document))
    assert [(dict(t)['RateApplicablePercent'], dict(t)['BasisAmount'], dict(t)['CalculatedAmount']) for t in header] == [
        ('5.50', '0.09', '0.00'), ('19.00', '100.00', '19.00')]
    subtotals, _lines = ubl_taxes(ubl_of(world, document))
    assert [(basis, amount, dict(category)['Percent']) for basis, amount, category in subtotals] == [('0.09', '0.00', '5.5'), ('100.00', '19.00', '19')]


def test_the_pdf_prints_the_5_5_percent_row_with_0_00_and_the_embedded_cii_says_the_same(world):
    document = mixed_with_5_5(world)

    pdf = invoice_pdf(world, document)

    lines = page_lines(pdf)
    assert lines_after(lines, HEADER, 7) == [
        HEADER,
        '1 1,00 Stk. Beratung 19 % 100,00 EUR 100,00 EUR',
        '2 1,00 Stk. Kleinteil 5,5 % 0,09 EUR 0,09 EUR',
        'Summe netto: 100,09 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'zzgl. 5,5 % MwSt. auf 0,09 EUR: 0,00 EUR',
        'Gesamtbetrag: 119,09 EUR']
    header, _lines = cii_taxes(embedded_xml(pdf))
    assert [(dict(t)['RateApplicablePercent'], dict(t)['CalculatedAmount']) for t in header] == [('5.50', '0.00'), ('19.00', '19.00')]


def test_a_document_without_a_snapshot_keeps_the_two_step_rounding_for_5_5_percent(world):
    """Die Gegenprobe: Dieselbe Position ohne Momentaufnahme (der ganze Bestand) rechnet zweistufig wie vor 2.8.0 – 0,01 statt 0,00 –, und die gespeicherte Summe
    (``item_groups`` ohne ``bp``) ist dieselbe."""
    document = make_document(world, [plain(1, 'Kleinteil', '0.09', 5.5)], '26R321', head_rate=0.055)
    stored = columns(world.db, document)

    model = model_of(world, document)

    assert [(t.category, t.rate, t.amount) for t in model.tax_breakdown] == [('S', Decimal('5.5'), to_minor('0.01'))]
    assert stored['TaxAmount'] == to_minor('0.01') and model.warnings == []
    assert [line.exact for line in model.lines] == [False]


def test_a_single_5_5_percent_document_with_a_snapshot_rounds_once(world):
    document = make_document(world, [standard(1, 'Kleinteil', '0.09', 550)], '26R322')
    stored = columns(world.db, document)

    model = model_of(world, document)

    assert stored['TaxAmount'] == 0 and stored['SumGross'] == to_minor('0.09')
    assert [(t.category, t.rate, t.amount) for t in model.tax_breakdown] == [('S', Decimal('5.5'), 0)]
    assert model.warnings == []


def test_a_standard_entry_with_rate_0_is_category_z_and_has_no_reason(world):
    """Die Art „Standard“ mit Satz 0 ist die Kategorie Z (Nullsatz): kein Befreiungsgrund, keine Lücke."""
    document = make_document(world, [standard(1, 'Lieferung', '40.00', 0)], '26R323')

    model = model_of(world, document)

    assert model.tax_breakdown == [TaxBreakdown('Z', Decimal(0), to_minor('40.00'), 0, '')]
    assert einvoice_gaps(model) == []
    assert 'ExemptionReason' not in dict(cii_taxes(cii_of(world, document))[0][0])


# ── Das PDF: Zeilen, Spalte „USt“, Hinweiszeile ──────────────────────────────────────────────────────────────────────────────────────

def test_the_pdf_of_19_percent_and_exempt_prints_steuerfrei_auf_the_column_ust_and_the_reason_under_the_block(world):
    pdf = invoice_pdf(world, mixed_with_exempt(world))

    lines = page_lines(pdf)
    assert lines_after(lines, HEADER, 8) == [
        HEADER,
        '1 1,00 Stk. Beratung 19 % 100,00 EUR 100,00 EUR',
        '2 1,00 Stk. Nachhilfestunde 0 % 80,00 EUR 80,00 EUR',
        'Summe netto: 180,00 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'steuerfrei auf 80,00 EUR: 0,00 EUR',
        'Gesamtbetrag: 199,00 EUR',
        REASON]
    assert not any('zzgl. 0 %' in line for line in lines)
    assert [text for text, _words in note_rows(pdf)] == [REASON]


def note_style(pdf):
    """Wo und in welcher Schriftgröße die erste Zeile unter dem Summenblock steht: ``(linker Rand, Abstand unter „Gesamtbetrag:“, Größen)``."""
    rows = page_rows(pdf)
    total = next(words for text, words in rows if text.startswith('Gesamtbetrag:'))
    (_text, words), *_rest = note_rows(pdf)
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        sizes = {round(char['size'], 1) for char in document.pages[0].chars if abs(char['top'] - words[0]['top']) < 1}
    return round(words[0]['x0'], 1), round(words[0]['top'] - total[0]['bottom'], 1), sizes


def test_the_reason_is_set_like_the_note_of_para_19_at_the_left_margin_in_9_pt_under_the_block(world):
    snapshot = invoice_pdf(world, make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R336'))
    para_19 = invoice_pdf(world, make_document(world, [plain(1, 'Nachhilfestunde', '80.00', 0, category='E')], '26R337', head_rate=-1))

    left, gap, sizes = note_style(snapshot)

    assert (left, gap, sizes) == note_style(para_19)
    assert abs(left - D.LEFT) < 0.5 and gap > 0 and sizes == {float(D._NOTE_SIZE)}


def test_a_single_rate_exempt_document_prints_the_old_block_with_0_percent_and_the_reason_under_it(world):
    """Ein Beleg mit EINEM Satz und Momentaufnahme: Summenblock wie bei jedem 0-%-Beleg („zzgl. 0% MwSt.: 0,00 EUR“), dazu der Grund unter dem Block."""
    pdf = invoice_pdf(world, make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R330'))

    assert lines_after(page_lines(pdf), HEADER_SINGLE_RATE, 6) == [
        HEADER_SINGLE_RATE,
        '1 1,00 Stk. Nachhilfestunde 80,00 EUR 80,00 EUR',
        'Summe netto: 80,00 EUR',
        'zzgl. 0% MwSt.: 0,00 EUR',
        'Gesamtbetrag: 80,00 EUR',
        REASON]


def test_a_single_rate_document_of_5_5_percent_with_a_snapshot_prints_the_rate_with_a_decimal_comma_and_no_note(world):
    """Die Steuerzeile eines Belegs mit EINEM Satz und Momentaufnahme nennt den Satz wie jede Satzgruppe mit Dezimalkomma („5,5%“ statt „5.5%“, ``tax_groups.pct_text``)."""
    pdf = invoice_pdf(world, make_document(world, [standard(1, 'Kleinteil', '20.00', 550)], '26R331'))

    lines = page_lines(pdf)
    assert lines_after(lines, 'Summe netto:', 3) == ['Summe netto: 20,00 EUR', 'zzgl. 5,5% MwSt.: 1,10 EUR', 'Gesamtbetrag: 21,10 EUR']
    assert note_rows(pdf) == []


def test_the_same_document_without_a_snapshot_keeps_the_point_in_the_rate_character_for_character(world):
    """Die Gegenprobe: Ein Beleg ohne Momentaufnahme – auch ein Bestandsbeleg mit 5,5 im Kopf – druckt die Zeile wie bisher, mit Punkt."""
    pdf = invoice_pdf(world, make_document(world, [plain(1, 'Kleinteil', '20.00', 5.5)], '26R338', head_rate=0.055))

    assert lines_after(page_lines(pdf), 'Summe netto:', 3) == ['Summe netto: 20,00 EUR', 'zzgl. 5.5% MwSt.: 1,10 EUR', 'Gesamtbetrag: 21,10 EUR']


@pytest.mark.parametrize('bp, expected', [(1900, '19'), (700, '7'), (1000, '10'), (1070, '10,7'), (1925, '19,25'), (100, '1'), (0, '0')])
def test_the_rate_of_the_single_rate_row_is_printed_with_a_comma_and_without_superfluous_zeros(world, bp, expected):
    """Auch bei Sätzen, die ``:g`` mit Punkt (oder, bei 10, als Zehnerpotenz) schriebe: ganze Prozent ohne Komma, sonst mit Komma und ohne Nullen am Ende."""
    pdf = invoice_pdf(world, make_document(world, [standard(1, 'Position', '20.00', bp)], f'26R34{bp}'))

    assert any(line.startswith(f'zzgl. {expected}% MwSt.:') for line in page_lines(pdf))


def test_the_quote_prints_the_rate_of_the_single_rate_row_with_a_decimal_comma_only_with_a_snapshot(world):
    snapshot = quote_pdf(world, make_document(world, [standard(1, 'Kleinteil', '20.00', 550)], '26A310', quote=True))
    without = quote_pdf(world, make_document(world, [plain(1, 'Kleinteil', '20.00', 5.5)], '26A311', quote=True, head_rate=0.055))

    assert lines_after(page_lines(snapshot), 'Summe netto:', 3)[1] == 'zzgl. 5,5% MwSt.: 1,10 EUR'
    assert lines_after(page_lines(without), 'Summe netto:', 3)[1] == 'zzgl. 5.5% MwSt.: 1,10 EUR'


def test_a_document_without_a_snapshot_prints_no_note_and_the_reason_is_none(world):
    document = make_document(world, [plain(1, 'Beratung', '100.00', 19)], '26R332')

    assert exemption_note(world.db.get_invoice_items(document)) is None
    assert note_rows(invoice_pdf(world, document)) == []


def test_a_document_after_para_19_prints_its_note_as_before(world):
    pdf = invoice_pdf(world, make_document(world, [plain(1, 'Beratung', '100.00', 0, category='E')], '26R333', head_rate=-1))

    assert [text for text, _words in note_rows(pdf)] == [PARA_19_NOTE]


def test_an_exempt_group_without_a_reason_prints_no_note(world):
    pdf = invoice_pdf(world, mixed_with_exempt(world, '26R334', reason=None))

    lines = page_lines(pdf)
    assert 'steuerfrei auf 80,00 EUR: 0,00 EUR' in lines and note_rows(pdf) == []


def test_an_exempt_group_and_a_standard_group_with_rate_0_are_labelled_differently(world):
    """Nur die Momentaufnahme eines steuerfreien Eintrags heißt „steuerfrei“; ein Nullsatz der Art „Standard“ bleibt „zzgl. 0 % MwSt. auf …“ (Kategorie Z)."""
    document = make_document(world, [standard(1, 'Beratung', '100.00', 1900), standard(2, 'Lieferung', '40.00', 0)], '26R335', mixed=1)

    lines = page_lines(invoice_pdf(world, document))

    assert 'zzgl. 0 % MwSt. auf 40,00 EUR: 0,00 EUR' in lines
    assert not any(line.startswith('steuerfrei') for line in lines)


# ── Ein langer Grund: bricht um, bleibt auf der Seite, geht nie verloren ─────────────────────────────────────────────────────────────

LONG_REASON = ' '.join(['Beispielleistung ohne Umsatzsteuer'] * 8 + ['x' * 20])         # genau 300 Zeichen, das Maximum eines Eintrags


@pytest.mark.parametrize('reason', [LONG_REASON, 'Sehr' + 'lang' * 73 + 'Ende'], ids=['Wörter', 'ein Wort ohne Leerzeichen'])
def test_a_reason_of_300_characters_wraps_inside_the_margins_and_nothing_is_lost(world, reason):
    """Gemessen an der Schrift des fertigen PDF (nicht an der Tabelle des Codes): Jede Zeile liegt zwischen dem linken und dem rechten Rand, und der Text ist
    vollständig – mit Wörtern bricht er an den Leerzeichen um, ein einziges Wort bricht mitten im Wort um."""
    assert len(reason) == 300
    pdf = invoice_pdf(world, mixed_with_exempt(world, '26R340', reason=reason))

    rows = note_rows(pdf)

    assert len(rows) >= 3
    assert ''.join(text.replace(' ', '') for text, _words in rows) == reason.replace(' ', '')
    if ' ' in reason:
        assert ' '.join(text for text, _words in rows) == reason
    for _text, words in rows:
        assert words[0]['x0'] >= D.LEFT - 0.5 and words[-1]['x1'] <= D.RIGHT + 0.5
    assert page_count(pdf) == 1


def test_a_reason_of_300_characters_is_the_same_text_in_cii_and_ubl(world):
    document = mixed_with_exempt(world, '26R341', reason=LONG_REASON)

    assert dict(cii_taxes(cii_of(world, document))[0][0])['ExemptionReason'] == LONG_REASON
    assert dict(ubl_taxes(ubl_of(world, document))[0][0][2])['TaxExemptionReason'] == LONG_REASON


def _inside_the_margins(rows):
    """Jede Zeile liegt zwischen dem linken und dem rechten Rand (gemessen an der Schrift des fertigen PDF)."""
    for _text, words in rows:
        assert words[0]['x0'] >= D.LEFT - 0.5 and words[-1]['x1'] <= D.RIGHT + 0.5, (words[0]['x0'], words[-1]['x1'])


def _nothing_below_the_bottom(pdf):
    """Keine Textstelle des Inhalts unter dem unteren Rand (``PageFlow.bottom``, 120 pt von unten) – ohne die Fußzeile."""
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        for page in document.pages:
            body = [word for word in page.extract_words() if word['top'] < 740]
            assert all(word['bottom'] <= 842 - 120 + 3 for word in body)


@pytest.mark.parametrize('length', [301, 5000, 100000])
def test_a_reason_over_300_characters_ends_with_an_ellipsis_after_the_first_300_in_model_xml_and_pdf(world, length):
    """``check_tax_entry`` lässt höchstens 300 Zeichen zu, die SQL-Konsole mehr: Das PDF druckt die ersten 300 Zeichen (nach der Bereinigung) und ein „…“ – ein paar Zeilen, nie eine unter
    dem unteren Rand, nie still abgeschnitten –, und das XML trägt denselben gekürzten Text (EINE Stelle kürzt)."""
    reason = (LONG_REASON + 'y' + ' Weitere Hinweise zur steuerfreien Leistung' * 4000)[:length]
    expected = LONG_REASON + '…'
    assert len(reason) == length and len(expected) == MAX_REASON_LENGTH + 1
    document = mixed_with_exempt(world, '26R470', reason=reason)

    model = model_of(world, document)
    pdf = invoice_pdf(world, document)

    assert model.tax_breakdown[0].exemption_reason == expected and einvoice_gaps(model) == [] and model.warnings == []
    assert dict(cii_taxes(cii_of(world, document))[0][0])['ExemptionReason'] == expected
    assert dict(ubl_taxes(ubl_of(world, document))[0][0][2])['TaxExemptionReason'] == expected
    assert exemption_note(world.db.get_invoice_items(document)) == expected
    rows = note_rows(pdf)
    assert 3 <= len(rows) <= D._NOTE_MAX_LINES and ' '.join(text for text, _words in rows) == expected and rows[-1][0].endswith('…')
    _inside_the_margins(rows)
    _nothing_below_the_bottom(pdf)
    assert page_count(pdf) == 1


def test_a_reason_of_100000_characters_without_a_blank_is_cut_after_300_and_breaks_inside_the_word(world):
    document = mixed_with_exempt(world, '26R471', reason='x' * 100000)

    pdf = invoice_pdf(world, document)

    rows = note_rows(pdf)
    assert ''.join(text for text, _words in rows) == 'x' * MAX_REASON_LENGTH + '…'
    _inside_the_margins(rows)
    _nothing_below_the_bottom(pdf)
    assert dict(cii_taxes(cii_of(world, document))[0][0])['ExemptionReason'] == 'x' * MAX_REASON_LENGTH + '…'


WIDE_REASONS = {
    'Pfeile': ' → '.join(f'Schritt {number} der Prüfung' for number in range(1, 15))[:300],
    'Œ × 300': 'Œ' * 300,
    'Geviertstriche': ' — '.join(['Beispielleistung ohne Umsatzsteuer'] * 8)[:300],
    'Promille': ' '.join(['Anteil 12‰ vom Entgelt'] * 14)[:300],
    'Marke': ' '.join(['Produkt™ Beispiel'] * 20)[:300],
    'gemischt': ('Steuerfrei gemäß Testgesetz — Œuvre™ → Prüfung 5‰ ' * 6)[:300],
}


@pytest.mark.parametrize('name', list(WIDE_REASONS))
def test_a_reason_with_characters_the_helvetica_table_misjudges_stays_inside_the_margins_and_nothing_is_lost(world, name):
    """Gemessen an der Schrift des fertigen PDF: Der Pfeil steht als „->“ da (zwei Zeichen statt eines), „Œ“, „—“, „‰“ und „™“ sind ein ganzes Geviert breit (die Tabelle ``_W_REG`` zählt sie
    556) – früher lief ein solcher Grund bis zu 7,7 pt (Pfeile) und mehr als die Zeilenbreite („Œ“) über den rechten Rand. Jetzt liegt jede Zeile zwischen den Rändern, und kein Zeichen geht
    verloren."""
    reason = WIDE_REASONS[name]
    assert 0 < len(reason) <= MAX_REASON_LENGTH
    pdf = invoice_pdf(world, mixed_with_exempt(world, '26R472', reason=reason))

    rows = note_rows(pdf)

    _inside_the_margins(rows)
    assert ''.join(text.replace(' ', '') for text, _words in rows) == printed_text(reason).replace(' ', '')
    assert len(rows) <= D._NOTE_MAX_LINES and not rows[-1][0].endswith('…')
    _nothing_below_the_bottom(pdf)


def test_the_note_lines_measure_the_printed_text_with_the_widths_of_the_embedded_font():
    width = D.RIGHT - D.LEFT
    wide = D._note_lines('Œ' * 300, width, 9)
    arrows = D._note_lines('→ ' * 120, width, 9)                                        # steht als „-> “ da

    assert ''.join(wide) == 'Œ' * 300 and len(wide) == 6 and all(info_width(line, 9) <= width + 1e-6 for line in wide)
    assert ' '.join(arrows) == '-> ' * 119 + '->' and all(info_width(line, 9) <= width + 1e-6 for line in arrows)
    assert wide != D._wrap_description('Œ' * 300, width, 9, break_words=True)           # die Helvetica-Tabelle (556 statt 1000) hätte mehr Zeichen je Zeile angenommen


def test_the_longest_allowed_reason_made_of_the_widest_characters_never_reaches_the_limit_of_lines():
    """Ein Grund hat höchstens ``MAX_REASON_LENGTH`` + 1 Zeichen; aus Wörtern der breitesten Zeichen (kaum mehr als ein Geviert: das „@“) braucht er im ungünstigsten Fall zehn bis elf
    Zeilen – ein Wort aus 27 solcher Zeichen passt nur einmal in eine Zeile. Die Grenze von ``_NOTE_MAX_LINES`` (aus dem Verhältnis der Zeilenbreite zur Gesamtbreite: 12) schneidet
    daher nie einen zugelassenen Grund ab."""
    chance = random.Random(2811)
    width, widest, most = D.RIGHT - D.LEFT, 'Œ—‰™@WM…', 0
    for _ in range(400):
        pieces = [''.join(chance.choice(widest) for _ in range(chance.randint(18, 33))) for _ in range(20)]
        text = ' '.join(pieces)[:MAX_REASON_LENGTH].strip() + '…'
        lines = D._note_lines(text, width, 9)
        most = max(most, len(lines))
        assert ' '.join(lines) == printed_text(text) and all(info_width(line, 9) <= width + 1e-6 for line in lines)
    assert 9 <= most < D._NOTE_MAX_LINES


def test_a_note_longer_than_the_limit_of_lines_ends_with_an_ellipsis_in_the_last_line():
    lines = D._note_lines('Wort ' * 1000, D.RIGHT - D.LEFT, 9)

    assert len(lines) == D._NOTE_MAX_LINES and lines[-1].endswith('…') and not any(line.endswith('…') for line in lines[:-1])
    assert info_width(lines[-1], 9) <= D.RIGHT - D.LEFT + 1e-6


def _draw_with_note(count, note, start=586):
    """Die Tabelle mit ``count`` Positionen und dem Hinweis ``note`` auf einem ``PageFlow`` ohne Fußzeile; Rückgabe: ``(Seiten, PageFlow)``."""
    flow = D.PageFlow([[], [], []])
    flow.y = start
    positions = [{'pos': n, 'quantity': 1, 'unit': 'Stk.', 'description': f'Position {n}', 'price': 1, 'total': 1} for n in range(1, count + 1)]
    D.draw_item_table(flow, positions, tax_rate_pct=0, sum_net=count, tax_amount=0, sum_gross=count, tax_note=note)
    return flow.finish(), flow


_TEXT_OP = re.compile(r'^1 0 0 1 (-?[0-9.]+) (-?[0-9.]+) Tm \((.*)\) Tj$')


def _texts(ops):
    """``(x, y, Text)`` jeder Textstelle einer Seite, mit aufgelösten Klammern und Rückstrichen des PDF-Literals."""
    return [(float(m.group(1)), float(m.group(2)), re.sub(r'\\([()\\])', r'\1', m.group(3))) for m in map(_TEXT_OP.match, ops) if m]


@pytest.mark.parametrize('note', [LONG_REASON, 'Kurzer Grund ohne Umbruch', ' '.join(['Wort'] * 150), 'x' * 300],
                         ids=['300 Zeichen', 'eine Zeile', 'viele kurze Wörter', 'ein Wort ohne Leerzeichen'])
def test_the_note_never_runs_over_the_margins_nothing_stands_below_the_bottom_and_the_block_never_splits(note):
    """Für jede Zahl von Positionen: Summenblock und Hinweis stehen zusammen auf EINER Seite, keine Zeile unter dem unteren Rand, keine über dem rechten, und der
    Text ist vollständig (ein Wort ohne Leerzeichen bricht mitten im Wort um)."""
    for count in range(0, 70):
        pages, flow = _draw_with_note(count, note)
        block_pages = [index for index, ops in enumerate(pages) if any(text == 'Summe netto:' for _x, _y, text in _texts(ops))]
        assert len(block_pages) == 1, count
        texts = _texts(pages[block_pages[0]])
        total_y = next(y for _x, y, text in texts if text == 'Gesamtbetrag:')
        printed = [(x, y, text) for x, y, text in texts if y < total_y]
        assert printed and all(y >= flow.bottom for _x, y, _text in printed), count
        assert all(x == D.LEFT and D.text_width(text, 9) <= D.RIGHT - D.LEFT + 0.01 for x, _y, text in printed), count
        assert ''.join(text.replace(' ', '') for _x, _y, text in printed) == note.replace(' ', ''), count
        for ops in pages:
            assert all(y >= flow.bottom for _x, y, _text in _texts(ops)), count


def test_a_longer_note_moves_the_block_to_the_next_page_exactly_when_it_no_longer_fits():
    """Der Hinweis gehört zum Block: Mit der ersten Zahl von Positionen, bei der Block und langer Hinweis nicht mehr auf die Seite passen, geht beides auf die
    nächste – mit einem kurzen Hinweis passt der Block bei derselben Zahl noch."""
    def block_page(count, note):
        pages, _flow = _draw_with_note(count, note)
        return next(index for index, ops in enumerate(pages) if any(text == 'Summe netto:' for _x, _y, text in _texts(ops)))

    first = next(count for count in range(0, 70) if block_page(count, LONG_REASON) == 1)

    assert block_page(first - 1, LONG_REASON) == 0
    assert block_page(first, 'Kurzer Grund') == 0
    assert first > 0


def test_a_semicolon_in_the_reason_stays_a_semicolon_while_a_description_still_breaks_at_it():
    note = 'Steuerfrei nach dem Testgesetz; keine Umsatzsteuer'

    pages, _flow = _draw_with_note(2, note)

    assert note in [text for _x, _y, text in _texts(pages[0])]
    assert D._wrap_description('Beratung; zwei Stunden', 400, 9) == ['Beratung', 'zwei Stunden']                  # die Bezeichnung einer Position: wie immer
    assert D._note_lines(note, 400, 9) == [note]                                                                    # der Hinweis: das Semikolon bleibt ein Zeichen des Textes


def test_the_reason_is_printed_like_any_text_of_the_document_winansi_replacements_included(world):
    """Ein Zeichen außerhalb von WinAnsi (hier der Pfeil) ersetzt das PDF wie überall durch ein darstellbares Pendant; das XML behält es."""
    reason = 'Steuerfrei: Übergang → keine Umsatzsteuer'
    document = mixed_with_exempt(world, '26R342', reason=reason)

    (note,) = note_rows(invoice_pdf(world, document))

    assert note[0] == 'Steuerfrei: Übergang -> keine Umsatzsteuer'
    assert dict(cii_taxes(cii_of(world, document))[0][0])['ExemptionReason'] == reason


# ── Der Text aus der Datenbank im XML ────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_reason_goes_through_the_cleaning_of_the_e_invoice_in_model_cii_ubl_and_pdf(world):
    """Steuerzeichen fallen weg, Leerraum und Zeilenumbrüche werden ein Leerzeichen – überall derselbe Text (``_text``)."""
    dirty = 'Grund\x07 mit\tSteuerzeichen\nund   Umbruch\x00'
    document = mixed_with_exempt(world, '26R343', reason=dirty)
    clean = 'Grund mit Steuerzeichen und Umbruch'

    assert model_of(world, document).tax_breakdown[0].exemption_reason == clean
    assert dict(cii_taxes(cii_of(world, document))[0][0])['ExemptionReason'] == clean
    assert dict(ubl_taxes(ubl_of(world, document))[0][0][2])['TaxExemptionReason'] == clean
    assert exemption_note(world.db.get_invoice_items(document)) == clean
    assert [text for text, _words in note_rows(invoice_pdf(world, document))] == [clean]


def test_a_reason_with_markup_characters_is_escaped_and_the_xml_stays_well_formed(world):
    reason = 'Steuerfrei <b>nicht</b> & "so" \'ja\' > 0'
    document = mixed_with_exempt(world, '26R344', reason=reason)

    cii, ubl = cii_of(world, document), ubl_of(world, document)

    assert dict(cii_taxes(cii)[0][0])['ExemptionReason'] == reason                     # ET.fromstring: wohlgeformt, und der Text kommt unverändert zurück
    assert dict(ubl_taxes(ubl)[0][0][2])['TaxExemptionReason'] == reason
    assert b'<b>' not in cii and b'<b>' not in ubl


# ── Stornorechnung und Angebot ───────────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_credit_note_has_positive_amounts_in_the_xml_keeps_the_reason_and_prints_the_stored_negative_amounts(world):
    original = mixed_with_exempt(world, '26R350')
    storno = credit_note(world, original)

    model = model_of(world, storno)
    cii, ubl = cii_of(world, storno), ubl_of(world, storno)
    pdf = invoice_pdf(world, storno)

    assert model.type_code == '381' and model.warnings == []
    assert model.tax_breakdown == [TaxBreakdown('E', Decimal(0), to_minor('80.00'), 0, REASON),
                                   TaxBreakdown('S', Decimal(19), to_minor('100.00'), to_minor('19.00'), '')]
    assert [(dict(t)['CategoryCode'], dict(t)['BasisAmount'], dict(t)['CalculatedAmount']) for t in cii_taxes(cii)[0]] == [
        ('E', '80.00', '0.00'), ('S', '100.00', '19.00')]
    assert ET.fromstring(ubl).tag.endswith('}CreditNote') and ubl_taxes(ubl)[0][0][2][2] == ('TaxExemptionReason', REASON)
    lines = page_lines(pdf)
    assert lines_after(lines, 'Summe netto:', 4) == ['Summe netto: -180,00 EUR', 'zzgl. 19 % MwSt. auf -100,00 EUR: -19,00 EUR',
                                                     'steuerfrei auf -80,00 EUR: 0,00 EUR', 'Gesamtbetrag: -199,00 EUR']
    assert [text for text, _words in note_rows(pdf)] == [REASON]
    assert cii_taxes(embedded_xml(pdf))[0][0][2] == ('ExemptionReason', REASON)


def test_the_credit_note_of_the_0_09_euro_case_is_the_negated_invoice_to_the_cent(world):
    """Die Rundung ist spiegelbildlich: Die Gruppen der Stornorechnung sind auf den Cent die negierten der Rechnung (0,00 € bleibt 0,00 €)."""
    original = mixed_with_5_5(world, '26R351')
    storno = credit_note(world, original)
    stored = columns(world.db, storno)

    model = model_of(world, storno)

    assert stored['TaxAmount'] == -to_minor('19.00') and model.warnings == []
    assert [(t.basis, t.amount) for t in model.tax_breakdown] == [(to_minor('0.09'), 0), (to_minor('100.00'), to_minor('19.00'))]
    assert 'zzgl. 5,5 % MwSt. auf -0,09 EUR: 0,00 EUR' in page_lines(invoice_pdf(world, storno))


def test_the_quote_prints_the_steuerfrei_row_and_the_reason_like_the_invoice(world):
    quote = mixed_with_exempt(world, '26A301', quote=True)

    pdf = quote_pdf(world, quote)

    lines = page_lines(pdf)
    assert lines_after(lines, HEADER, 8) == [
        HEADER,
        '1 1,00 Stk. Beratung 19 % 100,00 EUR 100,00 EUR',
        '2 1,00 Stk. Nachhilfestunde 0 % 80,00 EUR 80,00 EUR',
        'Summe netto: 180,00 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'steuerfrei auf 80,00 EUR: 0,00 EUR',
        'Gesamtbetrag: 199,00 EUR',
        REASON]


def test_a_single_rate_exempt_quote_prints_the_reason_and_a_quote_after_para_19_prints_its_note(world):
    exempt_quote = quote_pdf(world, make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26A302', quote=True))
    para_19_quote = quote_pdf(world, make_document(world, [plain(1, 'Beratung', '100.00', 0, category='E')], '26A303', quote=True, head_rate=-1))

    assert [text for text, _words in note_rows(exempt_quote)] == [REASON]
    assert [text for text, _words in note_rows(para_19_quote)] == [PARA_19_NOTE]


# ── Ein steuerfreier Eintrag ohne Grund: eine Lücke, kein UBL ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('missing', [None, '', '   ', '\t\n'], ids=['NULL', 'leer', 'Leerzeichen', 'Steuerzeichen'])
def test_an_exempt_group_without_a_reason_has_no_text_but_a_gap(world, missing):
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00', reason=missing)], '26R360')

    model = model_of(world, document)

    assert model.tax_breakdown[0].exemption_reason == ''
    assert einvoice_gaps(model) == [Gap('Grund der Steuerbefreiung', 'en16931')]
    assert 'ExemptionReason' not in dict(cii_taxes(build_cii(model))[0][0])               # kein Text – und keiner zu § 19


def test_without_a_reason_the_pdf_is_made_but_no_ubl_is_written_and_the_error_names_the_gap(world):
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00', reason=None)], '26R361')

    result = generate_invoice_documents(world.db, document, with_xml=True)

    assert result['pdf_path'] and result['xml_path'] is None and result['incomplete'] is True
    assert 'Grund der Steuerbefreiung' in result['error'] and 'XRechnung (XML) nicht erstellt' in result['error']


def test_with_a_reason_the_ubl_is_written_and_carries_it(world):
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R362')

    result = generate_invoice_documents(world.db, document, with_xml=True)

    assert result['error'] is None and result['xml_path']
    with open(result['xml_path'], 'rb') as handle:
        assert ubl_taxes(handle.read())[0][0][2][2] == ('TaxExemptionReason', REASON)
    with open(result['pdf_path'], 'rb') as handle:
        assert cii_taxes(embedded_xml(handle.read()))[0][0][2] == ('ExemptionReason', REASON)


def test_the_gap_belongs_to_the_group_without_a_reason_not_to_a_document_after_para_19(world):
    document = make_document(world, [plain(1, 'Beratung', '100.00', 0, category='E')], '26R363', head_rate=-1)

    assert einvoice_gaps(model_of(world, document)) == []


# ── Zwei Gründe in einer Gruppe, fehlerhafte Werte ───────────────────────────────────────────────────────────────────────────────────

def reason_lines(out):
    """Die Zeilen des Server-Logs zu verschiedenen Befreiungsgründen."""
    return [line for line in out.splitlines() if DIFFERENT_REASONS_MESSAGE in line]


def test_positions_of_one_exempt_group_with_different_reasons_give_the_first_in_xml_and_pdf_and_one_line_in_the_log(world, capsys):
    """Die App speichert höchstens einen Grund je Gruppe; steht trotzdem ein zweiter da (SQL-Konsole), gilt der erste – in der E-Rechnung wie im PDF, derselbe Text –, und das Log
    sagt es mit EINER Zeile (bereinigte Belegnummer, fester Text): bei der Rechnung steht sie dort, wo das Modell gebaut wird, beim Angebot ohne Modell im PDF."""
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00', REASON), exempt(2, 'Lernmaterial', '20.00', OTHER_REASON)], '26R370')
    capsys.readouterr()

    model = model_of(world, document)

    assert model.tax_breakdown[0].exemption_reason == REASON and einvoice_gaps(model) == []
    assert model.warnings == [DIFFERENT_REASONS_MESSAGE]
    assert reason_lines(capsys.readouterr().out) == [f'E-Rechnung 26R370: {DIFFERENT_REASONS_MESSAGE}']
    assert exemption_note(world.db.get_invoice_items(document)) == REASON                  # dieselbe Funktion: das PDF druckt den Grund, den auch das XML trägt
    assert reason_lines(capsys.readouterr().out) == []                                      # ohne Belegnummer schreibt sie nichts
    pdf = invoice_pdf(world, document)
    assert [text for text, _words in note_rows(pdf)] == [REASON]
    assert cii_taxes(embedded_xml(pdf))[0][0][2] == ('ExemptionReason', REASON)
    assert len(reason_lines(capsys.readouterr().out)) == 1                                  # die Rechnung: eine Zeile je Bau des Modells, vom PDF kommt keine zweite
    quote = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00', REASON), exempt(2, 'Lernmaterial', '20.00', OTHER_REASON)], '26A370', quote=True)
    assert [text for text, _words in note_rows(quote_pdf(world, quote))] == [REASON]
    assert reason_lines(capsys.readouterr().out) == [f'PDF 26A370: {DIFFERENT_REASONS_MESSAGE}']


@pytest.mark.parametrize('garbage', ['abc', 550.5, -1, 10001, '', 1, 50, 99],
                         ids=['Text', 'Kommazahl', 'negativ', 'über 100 %', 'leerer Text', '0,01 %', '0,5 %', '0,99 %'])
def test_a_stored_value_that_is_no_valid_snapshot_counts_as_no_snapshot(world, garbage):
    """Die Spalte lässt sich per SQL-Konsole beschreiben: Nur eine ganze Zahl, die 0 ist oder von 100 (1 %) bis 10000 reicht, ist eine Momentaufnahme – ein Satz zwischen 0 und
    1 % lässt ``check_tax_entry`` nicht zu, ``rate_pct`` läse ihn als Bruch –, alles andere rechnet die Position aus ihrem ``TaxRate`` wie jede ohne, ohne Absturz und ohne dass
    ein Grund gedruckt würde."""
    document = make_document(world, [plain(1, 'Beratung', '100.00', 19)], '26R371')
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxRateBp = ?, TaxExemptReason = ? WHERE InvoiceId = ?', garbage, REASON, document)

    model = model_of(world, document)

    assert [(line.tax_category, line.tax_rate, line.exact) for line in model.lines] == [('S', Decimal(19), False)]
    assert model.tax_breakdown == [TaxBreakdown('S', Decimal(19), to_minor('100.00'), to_minor('19.00'), '')]
    assert exemption_note(world.db.get_invoice_items(document)) is None and note_rows(invoice_pdf(world, document)) == []


def _row(rate_bp=None, category='S', reason=None, rate=0, total='10.00', columns=14):
    """Eine Zeile aus ``InvoiceItems`` (``SELECT *``): ID, Beleg, Position, Artikel, Bezeichnung, Menge, Einheit, Preis, Netto, Art, Satz, Bp, Grund, Konto."""
    row = (1, 1, 1, None, 'Position', 1.0, 'C62', Decimal(total), Decimal(total), category, rate, rate_bp, reason, None)
    return row[:columns]


@pytest.mark.parametrize('value, expected', [(None, None), (0, 0), (100, 100), (1900, 1900), (10000, 10000), (10001, None), (-1, None), ('1900', None), (550.0, None),
                                             (True, None), (False, None), (1, None), (50, None), (99, None)])
def test_item_bp_reads_only_a_whole_number_that_is_0_or_from_100_to_10000(value, expected):
    """0 und 1 bis 100 Prozent (``MIN_RATE_BP`` bis ``MAX_RATE_BP``): so lässt ``check_tax_entry`` einen Satz zu. Ein Wert von 1 bis 99 – ein Satz unter 1 % – gilt wie jeder
    unlesbare als „keine Momentaufnahme“."""
    assert item_bp(_row(value)) == expected


def test_item_bp_of_a_short_row_is_none():
    """Das Werkzeug der Beispiele baut Zeilen ohne die neuen Spalten: Sie tragen keine Momentaufnahme."""
    assert item_bp(_row(550)) == 550
    assert item_bp(_row(550, columns=11)) is None and item_bp(_row(550, columns=10)) is None


@pytest.mark.parametrize('items, expected', [
    ([], None),
    ([_row(None, rate=19)], None),                                                                                    # ohne Momentaufnahme wird nichts gelesen
    ([_row(0, 'E', REASON)], REASON),
    ([_row(0, 'E', REASON), _row(0, 'E', REASON, total='3.00')], REASON),                                              # alle Positionen mit Satz 0, derselbe Grund
    ([_row(1900, 'S', rate=19), _row(0, 'E', REASON)], REASON),                                                       # die Position zu 19 % zählt nicht mit
    ([_row(0, 'E', REASON), _row(0, 'E', OTHER_REASON)], REASON),                                                     # verschiedene Gründe: der erste gilt
    ([_row(0, 'E', OTHER_REASON), _row(0, 'E', REASON)], OTHER_REASON),                                               # …in der Reihenfolge der Positionen
    ([_row(0, 'E', REASON), _row(None, 'S', rate=0)], REASON),                                                        # daneben eine Position mit Satz 0 ohne die Momentaufnahme: derselbe Grund wie im XML
    ([_row(0, 'E', REASON), _row(None, 'E', 'Anderer Text', rate=0)], REASON),                                        # eine Position 'E' ohne Momentaufnahme trägt keinen Grund bei
    ([_row(0, 'E', REASON), _row(None, 'S', rate=None)], REASON),                                                     # ohne Satz: unbekannt, zählt nicht
    ([_row(0, 'E', REASON), _row(None, 'S', rate='kein Satz')], REASON),                                              # unlesbar: ebenso, ohne Absturz
    ([_row(0, 'E', None)], None), ([_row(0, 'E', '  ')], None),                                                       # ohne Grund
    ([_row(0, 'E', None), _row(0, 'E', REASON)], REASON),                                                             # die erste ohne Grund, die zweite mit: der erste NICHT LEERE
    ([_row(0, 'E', '  '), _row(0, 'E', REASON), _row(0, 'E', OTHER_REASON)], REASON),
    ([_row(0, 'S', REASON)], None),                                                                                   # Nullsatz der Art „Standard“ (Kategorie Z)
    ([_row(0, 'E', REASON), _row(0, 'S', None)], REASON),                                                             # …neben einem steuerfreien Eintrag: der Grund des steuerfreien
    ([_row(0, 'S', OTHER_REASON), _row(0, 'E', REASON)], REASON),                                                     # …auch davor: ein Nullsatz der Art „Standard“ liefert nie einen Grund
    ([_row(1900, 'S', rate=19), _row(550, 'S', rate=5.5)], None),                                                     # Momentaufnahmen, aber keine mit Satz 0
    ([_row(0, 'E', ' Grund\tmit  Leerraum ')], 'Grund mit Leerraum'),                                                  # bereinigt wie in der E-Rechnung
    ([_row(0, 'E', b'Grund'), _row(0, 'E', 12345)], None),                                                            # kein Text: kein Grund (nie ``b'…'``)
    ([_row(50, 'E', REASON)], None),                                                                                  # 0,5 %: keine Momentaufnahme
])
def test_exemption_note_is_the_first_non_empty_reason_of_the_positions_with_an_exempt_snapshot(items, expected):
    """Der Grund, den das PDF druckt, ist der erste nicht leere der Positionen mit der Momentaufnahme eines steuerfreien Eintrags (Art ``E``, Satz 0) – was daneben mit dem Satz 0
    steht (ein Nullsatz der Art „Standard“, eine Position ohne Momentaufnahme), ändert ihn nicht. Es ist derselbe wie in der E-Rechnung (``exempt_reason``)."""
    assert exemption_note(items) == expected


@pytest.mark.parametrize('stored, expected', [
    ('Grund', 'Grund'),
    ('  Grund\t mit\nUmbruch\r\n ', 'Grund mit Umbruch'),
    ('Grund‮umgekehrt', 'Grundumgekehrt'),                          # U+202E (Kategorie Cf) fällt weg
    ('Grund​ohne﻿Lücke⁦', 'GrundohneLücke'),             # Nullbreite, Byte-Order-Mark, Isolierung (Cf)
    ('a\x0bb\x85c d\xa0e', 'a b c d e'),                           # Zeilenumbrüche und Leerzeichen jeder Art: ein Leerzeichen
    ('x\x00y\x07z\x1b[31m\x7f', 'xyz[31m'),                             # Steuerzeichen (Kategorie Cc) fallen weg
    ('Zeile￾￿', 'Zeile'),                                      # die in XML 1.0 verbotenen Nichtzeichen auch
    ('Straße — „Zitat“ → Œ ™', 'Straße — „Zitat“ → Œ ™'),              # Text bleibt, wie er ist (das PDF ersetzt beim Drucken)
    ('‮​ \t', ''),
    ('', ''), (None, ''), (b'Grund', ''), (12345, ''), (1.5, ''), (True, ''), (Decimal('1.5'), ''), (['Grund'], ''),
])
def test_the_reason_is_cleaned_in_one_place_only_text_counts_and_control_and_format_characters_go(stored, expected):
    found = exempt_reason([_row(0, 'E', stored)])

    assert found == ExemptReason(text=expected, differing=False)


def test_a_reason_over_the_limit_ends_with_an_ellipsis_after_the_first_300_characters_of_the_cleaned_text():
    """Gekürzt wird nach der Bereinigung (``MAX_REASON_LENGTH`` Zeichen vom bereinigten Text), nie in der Mitte eines Leerraums: Ein Leerzeichen vor dem „…“ fiele weg."""
    assert MAX_REASON_LENGTH == 300
    exactly = 'a' * 299 + 'b'
    assert exempt_reason([_row(0, 'E', exactly)]).text == exactly
    assert exempt_reason([_row(0, 'E', exactly + 'c')]).text == exactly + '…'
    assert exempt_reason([_row(0, 'E', 'a' * 299 + ' b' * 5)]).text == 'a' * 299 + '…'                  # der Schnitt fällt auf ein Leerzeichen: kein Leerzeichen vor dem „…“
    assert exempt_reason([_row(0, 'E', 'a\n' * 150 + 'b' * 20)]).text == ('a ' * 150)[:300].rstrip() + '…'      # gemessen am bereinigten Text, nicht am gespeicherten
    assert exempt_reason([_row(0, 'E', 'a' + ' ' * 400 + 'b' * 400)]).text == 'a ' + 'b' * 298 + '…'            # viel Leerraum davor: erst bereinigt, dann gekürzt
    assert exempt_reason([_row(0, 'E', 'a' + '​' * 1000 + 'b' * 400)]).text == 'a' + 'b' * 299 + '…'         # …ebenso Formatzeichen (Nullbreite)
    assert exempt_reason([_row(0, 'E', 'a' + '​' * 1000 + 'b' * 299)]).text == 'a' + 'b' * 299                 # genau 300 nach der Bereinigung: nicht gekürzt
    assert len(exempt_reason([_row(0, 'E', 'w ' * 200_000)]).text) == 300                                    # ein riesiger Wert: gekürzt, ohne dass die Laufzeit auffiele


@pytest.mark.parametrize('items, head_rate, expected', [
    ([_row(550, 'S', rate=5.5)], 0.055, '5,5'),
    ([_row(550, 'S', rate=5.5)], 5.5, '5,5'),                                      # Prozentzahl statt Bruch im Kopf
    ([_row(1900, 'S', rate=19)], 0.19, '19'),
    ([_row(1000, 'S', rate=10)], 0.1, '10'),                                       # nie als Zehnerpotenz
    ([_row(1070, 'S', rate=10.7)], 0.107, '10,7'),
    ([_row(1925, 'S', rate=19.25)], 0.1925, '19,25'),
    ([_row(0, 'E', rate=0)], 0, '0'),
    ([_row(0, 'E', rate=0)], 0.0, '0'),
    ([_row(550, 'S', rate=5.5), _row(None, 'S', rate=5.5)], 0.055, '5,5'),         # eine Position mit Momentaufnahme genügt
    ([_row(None, 'S', rate=5.5)], 0.055, None),                                    # ohne Momentaufnahme: wie immer
    ([_row('abc', 'S', rate=5.5)], 0.055, None),                                   # unlesbar: keine Momentaufnahme
    ([_row(50, 'S', rate=0.5)], 0.005, None),                                      # unter 1 %: keine Momentaufnahme
    ([_row(550, 'S', rate=5.5)], -1, None),                                        # ohne Umsatzsteuerausweis keine Steuerzeile
    ([_row(550, 'S', rate=5.5)], None, None),
    ([], 0.19, None),
])
def test_snapshot_rate_text_is_the_head_rate_with_a_decimal_comma_only_for_a_document_with_a_snapshot(items, head_rate, expected):
    assert snapshot_rate_text(items, head_rate) == expected


def _log_of(function, document, items, head_rate, capsys):
    capsys.readouterr()
    function(document, items, head_rate)
    return capsys.readouterr().out


@pytest.mark.parametrize('items, head_rate, expected', [
    ([_row(1900, 'S', rate=19)], 0.19, 0),                                         # passt: Prozentzahl
    ([_row(1900, 'S', rate=0.19)], 0.19, 0),                                       # … und Bruch
    ([_row(550, 'S', rate=5.5)], 0.055, 0),
    ([_row(1925, 'S', rate=19.25), _row(0, 'E', rate=0)], 0.1925, 0),
    ([_row(550, 'S', rate=19)], 0.19, 1),                                          # (a) die Momentaufnahme weicht vom ``TaxRate`` ab
    ([_row(1900, 'S', rate=7)], 0.19, 1),
    ([_row(550, 'S', rate=None)], 0.19, 1),                                        # (a) ohne ``TaxRate``
    ([_row(550, 'S', rate='abc')], 0.19, 1),                                       # (a) mit unlesbarem ``TaxRate``
    ([_row(0, 'S', rate=19)], 0.19, 1),
    ([_row('abc', 'E', rate=0)], 0.19, 1),                                         # (b) eine steuerfreie Position mit unlesbarer Momentaufnahme: Text
    ([_row(550.5, 'E', rate=0)], 0.19, 1),                                         # … Kommazahl
    ([_row(10001, 'E', rate=0)], 0.19, 1),                                         # … über 100 %
    ([_row(50, 'E', rate=0)], 0.19, 1),                                            # … unter 1 %
    ([_row(True, 'E', rate=0)], 0.19, 1),                                          # … ein Wahrheitswert
    ([_row('abc', 'S', rate=19)], 0.19, 0),                                        # unlesbar, aber nicht steuerfrei: kein Befund (die Position rechnet aus ihrem ``TaxRate``)
    ([_row(None, 'E', rate=0)], 0.19, 0),                                          # ``E`` ohne Momentaufnahme: Bestand, Werkzeug für Beispiele – wie bisher
    ([_row(None, 'S', rate=19)], 0.19, 0),
    ([_row('abc', 'E', rate=0)], -1, 0),                                           # ohne Umsatzsteuerausweis wird keine Momentaufnahme gelesen
    ([_row(550, 'S', rate=19)], -1, 0),
    ([_row(550, 'S', rate=19), _row('abc', 'E', rate=0), _row(10001, 'E', rate=0)], 0.19, 1),     # mehrere Befunde: trotzdem EINE Zeile
    ([_row(1900, 'S', rate=19), _row('abc', 'E', rate=0)], 0.19, 1),               # der Befund an der zweiten Position
    ([], 0.19, 0),
])
def test_one_line_in_the_log_if_a_snapshot_does_not_fit_its_rate_or_an_exempt_position_has_an_unreadable_one(items, head_rate, expected, capsys):
    out = _log_of(log_snapshot_findings, (1, '26R998'), items, head_rate, capsys)

    assert out.count(SNAPSHOT_FINDING_MESSAGE) == expected
    assert out == (f'PDF 26R998: {SNAPSHOT_FINDING_MESSAGE}\n' if expected else '')


def test_the_number_in_the_snapshot_line_is_cleaned_so_it_cannot_start_a_line_of_its_own(capsys):
    out = _log_of(log_snapshot_findings, (1, '26R997\nPDF 99: gefälscht\x1b[31m'), [_row(550, 'S', rate=19)], 0.19, capsys)

    assert out == f'PDF 26R997 PDF 99: gefälscht[31m: {SNAPSHOT_FINDING_MESSAGE}\n'


UNFLAGGED_NOTE = 'verschiedene Steuersätze'


@pytest.mark.parametrize('items, expected', [
    ([_row(None, rate=19), _row(None, rate=7)], 1),                                # ohne Momentaufnahme wie bisher
    ([_row(None, rate=19), _row(None, rate=0.19)], 0),
    ([_row(1900, rate=19), _row(700, rate=19)], 1),                                # gleicher ``TaxRate``, verschiedene Momentaufnahmen: die E-Rechnung nennt zwei Gruppen
    ([_row(1900, rate=19), _row(1900, rate=7)], 0),                                # verschiedener ``TaxRate``, gleiche Momentaufnahmen: dieselbe Gruppe
    ([_row(1900, rate='abc'), _row(700, rate=7)], 1),                              # die Momentaufnahme wird gelesen, nicht der unlesbare Satz
    ([_row(1900, rate=19), _row(None, rate=7)], 1),                                # eine mit, eine ohne
    ([_row(1900, rate=19), _row(None, rate=0.19)], 0),
    ([_row(1900, rate=19), _row('abc', rate=7)], 1),                               # eine unlesbare Momentaufnahme zählt nicht: ihr ``TaxRate``
    ([_row(0, 'E', rate=0), _row(0, 'S', rate=0)], 0),
    ([_row(0, 'E', rate=0), _row(1900, 'S', rate=19)], 1),
])
def test_the_line_for_a_document_without_the_flag_compares_the_rates_through_the_snapshot_where_there_is_one(items, expected, capsys):
    out = _log_of(log_unflagged_rates, (1, '26R996'), items, 0.19, capsys)

    assert out.count(UNFLAGGED_NOTE) == expected and out.count('\n') == expected


def test_per_item_rates_names_the_groups_that_are_exempt(world):
    flagged = mixed_with_exempt(world, '26R380')
    zero_rate = make_document(world, [standard(1, 'Beratung', '100.00', 1900), standard(2, 'Lieferung', '40.00', 0)], '26R381', mixed=1)
    without = make_document(world, [plain(1, 'Beratung', '100.00', 19), plain(2, 'Fachbuch', '50.00', 7)], '26R382', mixed=1)

    def rates_of(document_id):
        document = world.db.get_document(document_id, 'invoice')
        return per_item_rates(document, world.db.get_invoice_items(document_id), document[35])

    unflagged = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R383')

    assert rates_of(flagged).exempt == frozenset({Decimal(0)})
    assert [group.pct for group in rates_of(flagged).groups] == [Decimal(19), Decimal(0)]
    assert rates_of(flagged).item_rates == [Decimal(19), Decimal(0)]
    assert rates_of(zero_rate).exempt == frozenset()
    assert rates_of(without).exempt == frozenset()
    assert rates_of(unflagged) is None                                                                       # ohne Kennzeichen: wie immer


# ── Ein steuerfreier Eintrag neben einem Nullsatz oder einer Position ohne Momentaufnahme: EINE Quelle für den Grund ─────────────────

@pytest.mark.parametrize('flag', [1, None], ids=['mit Kennzeichen', 'ohne Kennzeichen'])
def test_an_exempt_entry_next_to_a_standard_entry_with_rate_0_gives_two_breakdowns_and_the_pdf_prints_the_reason(world, flag):
    """Steuerfrei (Kategorie E, mit Grund) und „Standard 0 %“ (Kategorie Z, ohne) in einem Beleg: Das XML hat zwei Aufschlüsselungen, das PDF zeigt beide in EINER Nullsatz-Gruppe –
    und druckt den Grund trotzdem unter dem Summenblock: Er ist der des Modells. Die Summenzeile heißt „zzgl. 0 % MwSt. auf …“, weil nicht alle Positionen der Gruppe steuerfrei sind
    (ohne das Kennzeichen steht die eine Steuerzeile eines Belegs mit EINEM Satz da)."""
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00'), standard(2, 'Lieferung', '40.00', 0)], f'26R41{flag or 0}', mixed=flag)

    model = model_of(world, document)
    pdf = invoice_pdf(world, document)

    assert model.tax_breakdown == [TaxBreakdown('E', Decimal(0), to_minor('80.00'), 0, REASON), TaxBreakdown('Z', Decimal(0), to_minor('40.00'), 0, '')]
    assert einvoice_gaps(model) == [] and model.warnings == []
    header, _lines = cii_taxes(cii_of(world, document))
    assert [(dict(t)['CategoryCode'], dict(t).get('ExemptionReason')) for t in header] == [('E', REASON), ('Z', None)]
    subtotals, _categories = ubl_taxes(ubl_of(world, document))
    assert [(dict(category)['ID'], dict(category).get('TaxExemptionReason')) for _basis, _amount, category in subtotals] == [('E', REASON), ('Z', None)]
    lines = page_lines(pdf)
    assert not any(line.startswith('steuerfrei') for line in lines)
    assert ('zzgl. 0 % MwSt. auf 120,00 EUR: 0,00 EUR' if flag else 'zzgl. 0% MwSt.: 0,00 EUR') in lines
    assert [text for text, _words in note_rows(pdf)] == [REASON]


def test_an_exempt_snapshot_and_an_exempt_row_without_a_snapshot_share_the_group_and_the_reason_of_the_snapshot(world):
    """Eine Position 'E' mit und eine ohne Momentaufnahme (Bestand, Werkzeug für Beispiele) bilden EINE Gruppe 'E': Das XML trägt den Grund der Momentaufnahme – und das PDF druckt ihn
    auch (früher nicht: Nicht alle Positionen mit dem Satz 0 trugen die Momentaufnahme). Die Summenzeile bleibt „zzgl. 0 % MwSt. auf …“."""
    document = make_document(world, [plain(1, 'Beratung', '100.00', 19), exempt(2, 'Nachhilfestunde', '10.00'), plain(3, 'Auslage', '10.00', 0, category='E')],
                             '26R420', mixed=1)

    model = model_of(world, document)
    pdf = invoice_pdf(world, document)

    assert [(t.category, t.basis, t.exemption_reason) for t in model.tax_breakdown] == [('E', to_minor('20.00'), REASON), ('S', to_minor('100.00'), '')]
    assert einvoice_gaps(model) == []
    lines = page_lines(pdf)
    assert 'zzgl. 0 % MwSt. auf 20,00 EUR: 0,00 EUR' in lines and not any(line.startswith('steuerfrei') for line in lines)
    assert [text for text, _words in note_rows(pdf)] == [REASON]


@pytest.mark.parametrize('reasons, expected', [
    ([None, REASON], REASON), (['', REASON], REASON), (['  \t', REASON, OTHER_REASON], REASON), ([REASON, None], REASON),
    ([REASON, OTHER_REASON], REASON), ([OTHER_REASON, REASON], OTHER_REASON), ([None, None, OTHER_REASON], OTHER_REASON),
], ids=['erste ohne Grund', 'erste leer', 'erste nur Leerraum', 'zweite ohne Grund', 'verschiedene Gründe', 'verschiedene, umgekehrt', 'dritte mit Grund'])
def test_the_first_non_empty_reason_of_the_exempt_positions_stands_in_xml_and_pdf_and_leaves_no_gap(world, reasons, expected):
    """Die steuerfreie Gruppe trägt den ERSTEN NICHT LEEREN Grund ihrer Positionen – früher war es der der ersten Position (leer: Lücke, obwohl ein Grund da ist) bzw. im PDF gar keiner."""
    positions = [plain(1, 'Beratung', '100.00', 19)] + [exempt(number, f'Leistung {number}', '10.00', reason) for number, reason in enumerate(reasons, 2)]
    document = make_document(world, positions, '26R430', mixed=1)

    model = model_of(world, document)

    assert model.tax_breakdown[0].exemption_reason == expected and einvoice_gaps(model) == []
    assert bool(model.warnings) == (len({reason for reason in reasons if reason and reason.strip()}) > 1)         # verschieden heißt: zwei verschiedene NICHT LEERE Gründe
    assert dict(cii_taxes(cii_of(world, document))[0][0])['ExemptionReason'] == expected
    assert dict(ubl_taxes(ubl_of(world, document))[0][0][2])['TaxExemptionReason'] == expected
    assert [text for text, _words in note_rows(invoice_pdf(world, document))] == [expected]


EXEMPT_COMBINATIONS = {
    'nur steuerfrei': [exempt(1, 'A', '10.00')],
    'steuerfrei und Standard 0 %': [exempt(1, 'A', '10.00'), standard(2, 'B', '10.00', 0)],
    'Standard 0 % und steuerfrei': [standard(1, 'B', '10.00', 0), exempt(2, 'A', '10.00')],
    'steuerfrei mit und ohne Momentaufnahme': [exempt(1, 'A', '10.00'), plain(2, 'B', '10.00', 0, category='E')],
    'ohne und mit Momentaufnahme': [plain(1, 'B', '10.00', 0, category='E'), exempt(2, 'A', '10.00')],
    'erster ohne Grund': [exempt(1, 'A', '10.00', None), exempt(2, 'B', '10.00')],
    'verschiedene Gründe': [exempt(1, 'A', '10.00'), exempt(2, 'B', '10.00', OTHER_REASON)],
    'ohne Grund': [exempt(1, 'A', '10.00', None)],
    'nur Standard 0 %': [standard(1, 'B', '10.00', 0)],
    'Art E ohne Momentaufnahme': [plain(1, 'B', '10.00', 0, category='E')],
    '19 % und steuerfrei': [plain(1, 'B', '10.00', 19), exempt(2, 'A', '10.00')],
    '19 %, Standard 0 % und steuerfrei': [standard(1, 'B', '10.00', 1900), standard(2, 'C', '10.00', 0), exempt(3, 'A', '10.00')],
}


@pytest.mark.parametrize('flag', [1, None], ids=['mit Kennzeichen', 'ohne Kennzeichen'])
@pytest.mark.parametrize('name', list(EXEMPT_COMBINATIONS))
def test_the_pdf_prints_exactly_the_reason_the_model_carries_for_its_exempt_group_from_a_snapshot(world, name, flag):
    """Die EINE Quelle: Trägt das Modell für seine Gruppe 'E' einen Grund aus der Momentaufnahme einer Position, druckt das PDF genau den; sonst druckt es nichts (der Hinweis auf § 19
    einer Gruppe 'E' ohne Momentaufnahme gehört dem Beleg nach § 19, nicht dem PDF eines Belegs mit Steuerausweis). Gleich, was daneben steht und mit welchem Kennzeichen."""
    positions = EXEMPT_COMBINATIONS[name]
    document = make_document(world, positions, f'26R44{flag or 0}', mixed=flag)

    model = model_of(world, document)
    pdf = invoice_pdf(world, document)

    from_snapshot = any(position.get('tax_rate_bp') == 0 and position['tax_category'] == 'E' for position in positions)
    model_reason = next((t.exemption_reason for t in model.tax_breakdown if t.category == 'E'), '')
    assert [text for text, _words in note_rows(pdf)] == ([model_reason] if from_snapshot and model_reason else [])
    assert exemption_note(world.db.get_invoice_items(document)) == ((model_reason or None) if from_snapshot else None)


def test_a_document_after_para_19_whose_payment_terms_cite_the_law_prints_no_reason_even_with_an_exempt_snapshot(world):
    """Der Hinweis nach § 19 UStG hat Vorrang vor jedem Befreiungsgrund: Zitiert der Zahlungshinweis das Gesetz schon, druckt das PDF unter dem Summenblock gar nichts – wie bisher, nie den
    Grund einer Momentaufnahme, die ein solcher Beleg gar nicht tragen dürfte –, und das XML trägt § 19."""
    terms = 'Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.'
    document = make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26R441', head_rate=-1, payment_terms=terms)

    pdf = invoice_pdf(world, document)

    lines = page_lines(pdf)
    assert any(line.startswith('Gemäß § 19 UStG') for line in lines) and not any(REASON in line for line in lines)
    assert not any(line.startswith('Es gilt die Steuerbefreiung') for line in lines)                  # auch der Standardsatz nicht: Der Text des Nutzers zitiert das Gesetz schon
    assert model_of(world, document).tax_breakdown[0].exemption_reason == PARA_19_NOTE
    assert cii_taxes(embedded_xml(pdf))[0][0][2] == ('ExemptionReason', PARA_19_NOTE)
    assert ubl_taxes(ubl_of(world, document))[0][0][2][2] == ('TaxExemptionReason', PARA_19_NOTE)


def test_a_quote_after_para_19_whose_intro_cites_the_law_prints_no_reason_even_with_an_exempt_snapshot(world):
    """Dasselbe beim Angebot (Einleitung statt Zahlungshinweis): Der Hinweis nach § 19 UStG – hier vom Text des Nutzers getragen – hat Vorrang, der Grund einer Momentaufnahme steht nie
    unter dem Summenblock eines Belegs ohne Umsatzsteuerausweis."""
    intro = 'Gemäß § 19 UStG wird keine Umsatzsteuer berechnet.'

    pdf = quote_pdf(world, make_document(world, [exempt(1, 'Nachhilfestunde', '80.00')], '26A441', quote=True, head_rate=-1, intro_text=intro))

    lines = page_lines(pdf)
    assert any(line.startswith('Gemäß § 19 UStG') for line in lines) and not any(REASON in line for line in lines)
    assert not any(line.startswith('Es gilt die Steuerbefreiung') for line in lines)


def test_a_stored_reason_that_is_no_text_is_no_reason_and_never_a_python_literal_in_a_document(world):
    """Ein Blob in der Spalte (SQL-Konsole, zurückgespielte Sicherung): kein Grund – eine Lücke der Prüfung, kein Hinweis im PDF, und nirgends ``b'…'`` im XML. (Eine Zahl wandelt die
    Spalte beim Speichern in Text.)"""
    document = mixed_with_exempt(world, '26R442')
    run_sql(world.db, 'UPDATE InvoiceItems SET TaxExemptReason = ? WHERE InvoiceId = ? AND Position = 2', sqlite3.Binary(b'Grund\xff\x00'), document)

    model = model_of(world, document)

    assert model.tax_breakdown[0].exemption_reason == '' and einvoice_gaps(model) == [Gap('Grund der Steuerbefreiung', 'en16931')]
    assert note_rows(invoice_pdf(world, document)) == []
    for xml in (cii_of(world, document), ubl_of(world, document)):
        assert b"b'" not in xml and b'ExemptionReason' not in xml


# ── Der Satz der Momentaufnahme geht dem ``TaxRate`` vor – und eine Gruppe ohne Momentaufnahme an einer Position rundet zweistufig ──────

def snapshot_beats_the_rate(world, number='26R400', **over):
    """100,00 € zu 19 % (ohne Momentaufnahme) und 20,00 € mit der Momentaufnahme 5,5 %, deren ``TaxRate`` 19 sagt: netto 120,00, Steuer 19,00 + 1,10, brutto 140,10."""
    return make_document(world, [plain(1, 'Beratung', '100.00', 19), snapshot_over_rate(2, 'Kleinteil', '20.00', 550, 19)], number, mixed=1, **over)


def test_the_snapshot_goes_before_the_tax_rate_of_the_position_in_model_cii_ubl_and_pdf(world, capsys):
    document = snapshot_beats_the_rate(world)
    stored = columns(world.db, document)
    assert (stored['SumNet'], stored['TaxAmount'], stored['SumGross']) == (to_minor('120.00'), to_minor('20.10'), to_minor('140.10'))
    capsys.readouterr()

    model = model_of(world, document)
    pdf = invoice_pdf(world, document)

    assert [line.tax_rate for line in model.lines] == [Decimal(19), Decimal('5.5')]
    assert [(t.category, t.rate, t.basis, t.amount) for t in model.tax_breakdown] == [
        ('S', Decimal('5.5'), to_minor('20.00'), to_minor('1.10')), ('S', Decimal(19), to_minor('100.00'), to_minor('19.00'))]
    assert (model.totals.tax_total, model.totals.grand_total) == (stored['TaxAmount'], stored['SumGross']) and model.warnings == []
    header, lines = cii_taxes(cii_of(world, document))
    assert [(dict(t)['RateApplicablePercent'], dict(t)['BasisAmount'], dict(t)['CalculatedAmount']) for t in header] == [('5.50', '20.00', '1.10'), ('19.00', '100.00', '19.00')]
    assert [dict(line)['RateApplicablePercent'] for line in lines] == ['19.00', '5.50']
    subtotals, line_categories = ubl_taxes(ubl_of(world, document))
    assert [(basis, amount, dict(category)['Percent']) for basis, amount, category in subtotals] == [('20.00', '1.10', '5.5'), ('100.00', '19.00', '19')]
    assert [dict(line)['Percent'] for line in line_categories] == ['19', '5.5']
    assert lines_after(page_lines(pdf), HEADER, 7) == [
        HEADER,
        '1 1,00 Stk. Beratung 19 % 100,00 EUR 100,00 EUR',
        '2 1,00 Stk. Kleinteil 5,5 % 20,00 EUR 20,00 EUR',                         # die Spalte „USt“ nennt 5,5 %, nicht den ``TaxRate`` 19
        'Summe netto: 120,00 EUR',
        'zzgl. 19 % MwSt. auf 100,00 EUR: 19,00 EUR',
        'zzgl. 5,5 % MwSt. auf 20,00 EUR: 1,10 EUR',
        'Gesamtbetrag: 140,10 EUR']
    embedded_header, _lines = cii_taxes(embedded_xml(pdf))                      # das eingebettete XML des PDF nennt dasselbe
    assert [(dict(t)['RateApplicablePercent'], dict(t)['CalculatedAmount']) for t in embedded_header] == [('5.50', '1.10'), ('19.00', '19.00')]
    out = capsys.readouterr().out
    assert out.count(SNAPSHOT_FINDING_MESSAGE) == 1 and f'PDF 26R400: {SNAPSHOT_FINDING_MESSAGE}' in out        # und eine Zeile im Log: Die Momentaufnahme weicht vom ``TaxRate`` ab


def test_the_quote_writes_the_line_about_a_snapshot_that_does_not_fit_its_rate_too(world, capsys):
    document = snapshot_beats_the_rate(world, '26A402', quote=True)
    capsys.readouterr()

    pdf = quote_pdf(world, document)

    assert capsys.readouterr().out.count(f'PDF 26A402: {SNAPSHOT_FINDING_MESSAGE}\n') == 1
    assert '2 1,00 Stk. Kleinteil 5,5 % 20,00 EUR 20,00 EUR' in page_lines(pdf)


def test_a_document_whose_snapshot_fits_its_rates_writes_no_line_about_findings(world, capsys):
    capsys.readouterr()

    invoice_pdf(world, mixed_with_5_5(world, '26R401'))
    quote_pdf(world, mixed_with_exempt(world, '26A401', quote=True))

    assert SNAPSHOT_FINDING_MESSAGE not in capsys.readouterr().out


def test_a_group_with_one_position_with_and_one_without_a_snapshot_rounds_in_two_steps_in_all_four_outputs_and_in_the_bookings(world):
    """Eine Gruppe rechnet einstufig nur, wenn ALLE ihre Positionen eine Momentaufnahme tragen: Hier trägt die zweite keine, also gilt die Rundung von vor 2.8.0 – 0,09 € zu 5,5 %
    ergeben 0,01 € (einstufig wären es 0,00 €). Modell, CII, UBL und PDF nennen dasselbe, die Summen des Belegs auch – und es sind die Gruppen, die ``tax_groups.mixed_groups`` den
    Buchungen gibt."""
    document = make_document(world, [standard(1, 'Kleinteil', '0.09', 550), plain(2, 'Zugabe', '0.00', 5.5)], '26R402', mixed=1, head_rate=0.055)
    stored = columns(world.db, document)
    rows = query_rows(world.db, 'SELECT TaxRate, TotalNet, TaxRateBp FROM InvoiceItems WHERE InvoiceId = ? ORDER BY Position', document)
    booked = mixed_groups(stored['SumNet'], stored['TaxAmount'], stored['SumGross'], [(row['TaxRate'], row['TotalNet'], row['TaxRateBp']) for row in rows])
    assert stored['TaxAmount'] == to_minor('0.01') and stored['SumGross'] == to_minor('0.10')

    model = model_of(world, document)
    pdf = invoice_pdf(world, document)

    assert [(group.pct, group.net, group.tax) for group in booked] == [(Decimal('5.5'), to_minor('0.09'), to_minor('0.01'))]
    assert [(t.category, t.rate, t.basis, t.amount) for t in model.tax_breakdown] == [('S', Decimal('5.5'), to_minor('0.09'), to_minor('0.01'))]
    assert [line.exact for line in model.lines] == [True, False] and model.warnings == []
    header, _lines = cii_taxes(cii_of(world, document))
    assert [(dict(t)['RateApplicablePercent'], dict(t)['BasisAmount'], dict(t)['CalculatedAmount']) for t in header] == [('5.50', '0.09', '0.01')]
    subtotals, _categories = ubl_taxes(ubl_of(world, document))
    assert [(basis, amount, dict(category)['Percent']) for basis, amount, category in subtotals] == [('0.09', '0.01', '5.5')]
    lines = page_lines(pdf)
    assert 'zzgl. 5,5 % MwSt. auf 0,09 EUR: 0,01 EUR' in lines and 'Gesamtbetrag: 0,10 EUR' in lines
    assert cii_taxes(embedded_xml(pdf))[0][0][0] == ('CalculatedAmount', '0.01')


def test_the_same_group_with_a_snapshot_on_every_position_rounds_in_one_step(world):
    """Die Gegenprobe zum Fall oben: Trägt auch die zweite Position die Momentaufnahme, rechnet die Gruppe einstufig – 0,00 € – in allen Ausgaben und in den Summen."""
    document = make_document(world, [standard(1, 'Kleinteil', '0.09', 550), standard(2, 'Zugabe', '0.00', 550)], '26R403', mixed=1, head_rate=0.055)
    stored = columns(world.db, document)

    model = model_of(world, document)

    assert stored['TaxAmount'] == 0 and [(t.rate, t.amount) for t in model.tax_breakdown] == [(Decimal('5.5'), 0)]
    assert 'zzgl. 5,5 % MwSt. auf 0,09 EUR: 0,00 EUR' in page_lines(invoice_pdf(world, document))


# ── Gegenprobe: ohne Momentaufnahme ändert sich nichts ───────────────────────────────────────────────────────────────────────────────

def test_without_a_snapshot_nothing_changes_in_model_cii_ubl_or_pdf_for_every_kind_of_document(world, capsys):
    """Positionen zu 19, 7, 5,5 und 0 %, mit und ohne Kennzeichen, nach § 19: Keine Position trägt eine Momentaufnahme, also ist jede Zeile ``exact`` falsch, jeder Grund
    der Hinweis auf § 19 (nur bei ``'E'``) und nirgends ein Hinweis unter dem Block außer dem zu § 19 – und das Log bleibt ohne Zeile zu Momentaufnahmen und Gründen. (Die Bytes
    selbst halten die gepinnten Hashes und die Golden-Dateien der übrigen Tests fest.)"""
    documents = {                                                                                        # Name → (Beleg, trägt er das Kennzeichen?)
        'ein Satz': (make_document(world, [plain(1, 'Beratung', '100.00', 19)], '26R390'), False),
        'zwei Sätze': (make_document(world, [plain(1, 'Beratung', '100.00', 19), plain(2, 'Fachbuch', '50.00', 7)], '26R391', mixed=1), True),
        'drei Sätze': (make_document(world, [plain(1, 'Beratung', '100.00', 19), plain(2, 'Fachbuch', '50.00', 7), plain(3, 'Kleinteil', '0.09', 5.5)],
                                     '26R392', mixed=1, head_rate=0.19), True),
        'null Prozent': (make_document(world, [plain(1, 'Lieferung', '40.00', 0)], '26R393', head_rate=0), False),
        'nach § 19': (make_document(world, [plain(1, 'Beratung', '100.00', 0, category='E')], '26R394', head_rate=-1), False),
    }
    for name, (document, flagged) in documents.items():
        model = model_of(world, document)
        assert not any(line.exact for line in model.lines), name
        assert model.warnings == [], name
        assert all(t.exemption_reason == (PARA_19_NOTE if t.category == 'E' else '') for t in model.tax_breakdown), name
        assert exemption_note(world.db.get_invoice_items(document)) is None, name
        assert snapshot_rate_text(world.db.get_invoice_items(document), columns(world.db, document)['TaxRate']) is None, name
        row = world.db.get_document(document, 'invoice')
        rates = per_item_rates(row, world.db.get_invoice_items(document), row[35])
        assert (rates is not None) == flagged and (rates is None or rates.exempt == frozenset()), name
        capsys.readouterr()
        pdf = invoice_pdf(world, document)
        out = capsys.readouterr().out
        assert SNAPSHOT_FINDING_MESSAGE not in out and DIFFERENT_REASONS_MESSAGE not in out, name
        assert [text for text, _words in note_rows(pdf)] == ([PARA_19_NOTE] if name == 'nach § 19' else []), name
        assert not [line for line in page_lines(pdf) if line.startswith('steuerfrei')], name


# ── Eigenschaft: Modell, CII, UBL und PDF stimmen mit den gespeicherten Summen überein ───────────────────────────────────────────────────

def br_co_17(net_minor, pct):
    """Die Steuer einer Gruppe nach der Norm (EN 16931, BR-CO-17), unabhängig vom Code: Basis mal Satz, einmal kaufmännisch auf den Cent."""
    return int((Decimal(net_minor) * pct / 10000).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 100


def test_model_cii_ubl_and_pdf_give_the_same_groups_and_the_stored_sums_for_random_documents(world):
    """Zufällige Belege (feste Folge der Zufallszahlen) mit Positionen zu 19, 7, 5,5, 10,7 und 19,25 % und steuerfrei – auch mit Beträgen, an denen die
    zweistufige Rundung einen Cent mehr ergäbe (0,09 € bei 5,5 %): Die Steuer jeder Gruppe ist die der Norm, die Summen sind die gespeicherten, CII und UBL
    nennen dasselbe wie das Modell, und das PDF druckt dieselben Gruppen."""
    chance = random.Random(2810)
    nets = ['0.09', '0.18', '0.27', '2.09', '4.09', '0.10', '1.00', '4.50', '12.34', '99.99', '250.50']
    kinds = [1900, 700, 550, 1070, 1925, None]                                                                    # None: steuerfrei
    for count in range(14):
        picked = chance.sample(kinds, chance.randint(1, 3))
        positions, number = [], 0
        for kind in picked:
            for _ in range(chance.randint(1, 3)):
                number += 1
                price, quantity = chance.choice(nets), chance.choice([1, 1, 2, 3])
                positions.append(exempt(number, f'Position {number}', price, quantity=quantity) if kind is None
                                 else standard(number, f'Position {number}', price, kind, quantity=quantity))
        document = make_document(world, positions, f'26R{500 + count}', mixed=1 if len(picked) > 1 else None)
        stored = columns(world.db, document)

        model = model_of(world, document)
        assert model.warnings == [], (count, picked)
        assert (model.totals.line_total, model.totals.tax_total, model.totals.grand_total) == (stored['SumNet'], stored['TaxAmount'], stored['SumGross'])
        for breakdown in model.tax_breakdown:
            assert breakdown.amount == (0 if breakdown.category == 'E' else br_co_17(breakdown.basis, breakdown.rate)), (count, picked, breakdown)
            assert bool(breakdown.exemption_reason) == (breakdown.category == 'E')
        header = cii_taxes(cii_of(world, document))[0]
        subtotals = ubl_taxes(ubl_of(world, document))[0]
        assert [(dict(t)['CategoryCode'], dict(t)['BasisAmount'], dict(t)['CalculatedAmount']) for t in header] == [
            (t.category, format(from_minor(t.basis), '.2f'), format(from_minor(t.amount), '.2f')) for t in model.tax_breakdown], (count, picked)
        assert [(basis, amount, dict(category)['ID']) for basis, amount, category in subtotals] == [
            (format(from_minor(t.basis), '.2f'), format(from_minor(t.amount), '.2f'), t.category) for t in model.tax_breakdown], (count, picked)

        lines = page_lines(invoice_pdf(world, document))
        assert f'Summe netto: {format_de(stored["SumNet"])} EUR' in lines and f'Gesamtbetrag: {format_de(stored["SumGross"])} EUR' in lines, (count, picked)
        if len(picked) > 1:
            printed = [line for line in lines if line.startswith(('zzgl.', 'steuerfrei auf'))]
            expected = [(f'steuerfrei auf {format_de(t.basis)} EUR: 0,00 EUR' if t.category == 'E'
                         else f'zzgl. {D._fmt_rate(t.rate)} MwSt. auf {format_de(t.basis)} EUR: {format_de(t.amount)} EUR')
                        for t in sorted(model.tax_breakdown, key=lambda t: -t.rate)]
            assert printed == expected, (count, picked)
