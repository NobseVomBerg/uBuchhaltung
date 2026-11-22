# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Hilfen der Schnappschuss-Tests zu Belegen mit EINEM Steuersatz (``tests/test_single_rate_snapshots.py``).

Je Steuersatz – 19 %, 7 %, 0 %, ohne Steuerausweis (§ 19 UStG, Kopf −1) und 5 % (ohne Wartekonto und ohne hinterlegtes Erlöskonto) –
entsteht ein eigener, frischer Bestand, in dem derselbe Ablauf läuft (``Scenario``): Rechnungen über die Wege der App (das Formular,
``handlers.handle_invoice_save``; Statuswechsel, Zuordnen und Entfernen von Zahlungen, Stornieren, Neu ausstellen, Berichtigen, die
Reparatur-Vorschau der Zahlungszuordnung – über dieselben Funktionen, die die Routen aufrufen), nie per Roh-SQL (die einzige Ausnahme ist
unten begründet: die Zeitpunkte des Protokolls). Danach wird festgehalten (``Scenario.snapshot``), was die App für diesen Satz gebucht und
ausgegeben hat: Buchungssätze, Zahlungszuordnungen, Rechnungen und Positionen, Protokoll und Fassungen, die Meldungen der Aufrufe, die
Prüfliste, die offenen Posten des Kunden, EÜR, Summen- und Saldenliste, Dashboard, der DATEV-Stapel und die Inhalte der Dokumente
(PDF-Content-Streams, eingebettete CII, UBL).

**Wiederholbar, ohne die Uhr.** Alle Daten liegen in der Vergangenheit (Rechnungen im März 2025, Zahlungen im April, Stornos im Mai).
Die Uhr ist festgesetzt (``frozen_clock``: ``datetime.date.today()``, ``datetime.datetime.now()``/``utcnow()``/``today()`` im Modul
``datetime`` – das fängt auch einen ``import datetime`` im Funktionskörper – und die Namen, die ``export/pdfa.py`` und
``export/einvoice_model.py`` beim Import gebunden haben; die Ortszeit steht auf UTC). Gemessen, wer im Ablauf die Uhr liest:
``db/invoice_payments.py`` (``_recalc_invoice``: ob eine Rechnung nach dem Entfernen ihrer Zahlung „überfällig“ oder „versendet“ ist),
``db/invoice_storno.py`` (``_today``: das Stornodatum liegt nicht in der Zukunft, die Ersatzrechnung trägt den heutigen Tag und nimmt ihre
Nummer aus dem Kreis dieses Jahres), ``db/invoice_repair.py`` (der Tag für den Status), ``db/invoices.py`` (``convert_quote_to_invoice``:
Datum und Nummernkreis der Rechnung aus dem Angebot), ``export/invoice_files.py`` (``archive_sent_documents``: der Tag im Dateinamen der
Fassungen), ``export/datev.py`` (der Zeitstempel im Vorlauf des Stapels), ``server/handlers.py`` (das Steuerdatum, das der DATEV-Export
an die Buchungen schreibt), ``export/pdf_quote.py`` (das Jahr im Ablageordner des Angebots), ``export/pdfa.py`` (die Erzeugungszeit in den
Metadaten der PDF) und ``document_parser.py`` (der Zeitstempel im SQL-Protokoll). Ein Lauf mit vorgestellter Systemuhr (Jahreswechsel 2031)
ergibt denselben Stand; ohne die festgesetzte Uhr weicht er ab (schon die Nummer der Ersatzrechnung, ihr Kreis ist der des Jahres 2031).

Was SQLite selbst stempelt (``CURRENT_TIMESTAMP`` in ``CreatedAt``, ``UpdatedAt``, ``SentAt``, ``ChangedAt``, ``ArchivedAt``), wird nicht
festgehalten – bis auf eine Stelle: Der Hinweis einer berichtigten Fassung nennt das Datum der Berichtigung und das der ersetzten Fassung
(``db.invoice_versions.correction_notice``, gelesen aus ``InvoiceCorrections.ChangedAt``) und steht im PDF, in der CII (BT-22) und im UBL.
Diese Zeitpunkte lassen sich in SQLite nicht stellen; ``Scenario._pinned_protocol`` setzt sie deshalb unmittelbar vor dem Erzeugen des Dokuments
der neuen Fassung auf feste Werte (der Versand am 18.03., die Berichtigung am 20.05.2025, wie ``tests/test_invoice_correction.py`` es tut) –
sonst hinge der Hash dieser drei Dokumente vom Tag des Laufs ab. (Die Zeitpunkte stehen auf 10:00 UTC: Mit der auf UTC gestellten Ortszeit
ist der Tag derselbe; wo es kein ``time.tzset`` gibt, bleibt die Zeitzone des Rechners, und 10:00 UTC fällt in allen Zonen von UTC−10 bis
UTC+13 auf denselben Kalendertag.) Gehasht werden die Content-Streams der PDF (nicht die Datei: sie trägt
Erzeugungszeit und Kennungen), die eingebettete ``factur-x.xml`` und die UBL-Datei; einen Zeitstempel der Erzeugung tragen die beiden
XML-Dokumente nicht.

**Was die Dateien enthalten:** Tabellenzeilen stehen als ein Text je Zeile (``Spalte=Wert | …``; leere Spalten entfallen – eine neue Spalte,
die leer bleibt, ändert nichts, wird sie gefüllt, zeigt es sich), die Beträge so, wie sie gespeichert sind (Minor Units). Auswertungen und
Rückgaben stehen in einer stabilen JSON-Form (Dataclasses und benannte Tupel als Dicts, ``Decimal`` und Fließkommazahlen als Text mit vier
Nachkommastellen, Mengen sortiert). Nur erfundene Namen, Nummern und Beträge.
"""
import contextlib
import csv
import dataclasses
import datetime
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import defaultdict, namedtuple
from decimal import Decimal
from unittest import mock

from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import resolve1, stream_value

import userctx
from export import einvoice_model, pdfa
from export.pdf_quote import generate_quote_pdf
from server import handlers
from tests.datev_helpers import check_of, datev_post, is_confirm_redirect, message_of

USER = 'anna'

# Die festgesetzte Uhr: ein Tag in der Vergangenheit, der nach allen Daten des Ablaufs liegt (Rechnungen März, Zahlungen April, Stornos Mai) –
# kein Stornodatum liegt in der Zukunft, und kein Tag hängt am Lauf. Nie „heute“
FIXED_DAY = (2025, 7, 15)
FIXED_MOMENT = (2025, 7, 15, 9, 30, 0)
YEAR = ('2025-01-01', '2025-12-31')
SUSA_MONTHS = ((2025, 4), (2025, 12))          # April: die Zahlungen und Erstattungen; Dezember: nur der Vortrag

# Ein Satz: Schlüssel in der Golden-Datei, Prozent je Position (wie die Maske sie schickt), Satz im Kopf als Bruch, „Umsatzsteuer
# ausweisen“ (aus: § 19, der Kopf wird als −1 gespeichert)
Case = namedtuple('Case', 'key item_rate head_rate show_tax')
CASES = {case.key: case for case in (
    Case('19', 19, 0.19, True), Case('7', 7, 0.07, True), Case('0', 0, 0.0, True),
    Case('par19', 0, 0.0, False), Case('5', 5, 0.05, True))}

SECTIONS = ('calls', 'bookings', 'payments', 'invoices', 'items', 'protocol', 'versions', 'findings', 'open_items', 'euer', 'susa', 'dashboard',
            'datev', 'documents', 'date_tax_after_export')

LEITWEG_ID = '04011000-1234512345-35'


# ═══ die festgesetzte Uhr ═════════════════════════════════════════════════════
@contextlib.contextmanager
def frozen_clock():
    """Uhr und Ortszeit festsetzen, solange der Block läuft. ``datetime.date`` und ``datetime.datetime`` im Modul ``datetime`` werden
    ersetzt (das fängt jeden Aufruf ``datetime.date.today()``, auch einen ``import datetime`` im Funktionskörper), dazu die Namen, die
    ``export.pdfa`` (``datetime``) und ``export.einvoice_model`` (``date``) beim Import gebunden haben. ``isinstance`` gegen die
    Ersatzklassen gilt auch für echte ``date``/``datetime``-Werte (``__instancecheck__``)."""
    real_date, real_datetime = datetime.date, datetime.datetime

    class DateMeta(type(real_date)):
        def __instancecheck__(cls, value):
            return isinstance(value, real_date)

    class DatetimeMeta(type(real_datetime)):
        def __instancecheck__(cls, value):
            return isinstance(value, real_datetime)

    class FrozenDate(real_date, metaclass=DateMeta):
        @classmethod
        def today(cls):
            return cls(*FIXED_DAY)

    class FrozenDatetime(real_datetime, metaclass=DatetimeMeta):
        @classmethod
        def now(cls, tz=None):
            return cls(*FIXED_MOMENT, tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return cls(*FIXED_MOMENT)

        @classmethod
        def today(cls):
            return cls(*FIXED_MOMENT)

    old_zone = os.environ.get('TZ')
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(datetime, 'date', FrozenDate))
        stack.enter_context(mock.patch.object(datetime, 'datetime', FrozenDatetime))
        stack.enter_context(mock.patch.object(pdfa, 'datetime', FrozenDatetime))
        stack.enter_context(mock.patch.object(einvoice_model, 'date', FrozenDate))
        if hasattr(time, 'tzset'):
            os.environ['TZ'] = 'UTC'
            time.tzset()
        try:
            yield
        finally:
            if hasattr(time, 'tzset'):
                if old_zone is None:
                    os.environ.pop('TZ', None)
                else:
                    os.environ['TZ'] = old_zone
                time.tzset()


# ═══ stabile Form ═════════════════════════════════════════════════════════════
def _four_places(number):
    return format(Decimal(repr(number)).quantize(Decimal('0.0001')), 'f')


def plain(value):
    """Ein Ergebnis in eine stabile JSON-Form bringen: Dataclasses und benannte Tupel als Dicts, Tupel und Listen als Listen, Mengen
    sortiert, ``Decimal`` und Fließkommazahlen als Text mit vier Nachkommastellen, Daten als ISO-Text; ``bool``/``int``/``str``/``None``
    bleiben."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal('0.0001')), 'f')
    if isinstance(value, float):
        return _four_places(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, tuple) and hasattr(value, '_asdict'):
        return {name: plain(item) for name, item in value._asdict().items()}
    if isinstance(value, dict):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((plain(item) for item in value), key=repr)
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    raise TypeError(f'nicht darstellbar: {type(value).__name__}')


def _cell(value):
    return repr(value) if isinstance(value, float) else str(value)


def table_lines(db, table, *, exclude=(), only=None, where='', order='ID'):
    """Die Zeilen einer Tabelle als je ein Text ``Spalte=Wert | …`` (Spalten in der Reihenfolge des Schemas; leere – ``NULL`` – entfallen;
    ``exclude``: Spalten, die nicht festgehalten werden, etwa Zeitstempel; ``only``: nur diese Spalten), sortiert nach ``order``. Lesend, roh,
    so wie gespeichert."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        cursor = con.execute(f'SELECT * FROM {table} {where} ORDER BY {order}')
        names = [column[0] for column in cursor.description]
        keep = [index for index, name in enumerate(names) if name not in exclude and (only is None or name in only)]
        return [' | '.join(f'{names[index]}={_cell(row[index])}' for index in keep if row[index] is not None)
                for row in cursor.fetchall()]


def json_text(value):
    """Die Golden-Datei: stabil geschrieben (sortierte Schlüssel, kein ASCII-Zwang, Einrückung 1, ``\\n`` am Ende)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, indent=1) + '\n'


def _short(value, limit=400):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + f'… ({len(text)} Zeichen)'


def _window(text, index, head=40, before=60, after=90):
    """Der Ausschnitt eines langen Textes um die Stelle ``index``, davor der Anfang (bei einer Tabellenzeile: ID und Datum)."""
    start = max(0, index - before)
    tail = '…' if index + after < len(text) else ''
    if start <= head:                                    # der Anfang gehört schon zum Ausschnitt
        return text[:index + after] + tail
    return text[:head] + ' … ' + text[start:index + after] + tail


# Schlüssel, die nur eine Zusammenfassung des Lesbaren sind: Prüfsumme, Hash, Länge. Sie werden zuletzt verglichen, damit die Meldung die
# lesbare Abweichung nennt und nicht zuerst den Hash
_SUMMARY_KEYS = frozenset({'sha256', 'check', 'bytes'})


def first_difference(expected, actual, path=''):
    """Die erste Stelle, an der zwei JSON-Strukturen abweichen: ``(Pfad, erwartet, tatsächlich)`` – oder ``None``, wenn sie gleich sind.
    Wörterbücher werden nach Schlüsseln verglichen (fehlende und neue Schlüssel sind die Abweichung; Hashes und Prüfsummen zuletzt), Listen
    Stelle für Stelle (eine andere Länge ist die Abweichung an der ersten Stelle, die in einer von beiden fehlt), Texte bis zum ersten
    abweichenden Zeichen (die Meldung zeigt den Ausschnitt dort)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(expected) | set(actual), key=lambda name: (name in _SUMMARY_KEYS, name)):
            if key not in expected:
                return f'{path}/{key}', '(fehlt in der Datei)', _short(actual[key])
            if key not in actual:
                return f'{path}/{key}', _short(expected[key]), '(fehlt im frischen Stand)'
            found = first_difference(expected[key], actual[key], f'{path}/{key}')
            if found:
                return found
        return None
    if isinstance(expected, list) and isinstance(actual, list):
        for index in range(max(len(expected), len(actual))):
            if index >= len(expected):
                return f'{path}[{index}]', '(fehlt in der Datei)', _short(actual[index])
            if index >= len(actual):
                return f'{path}[{index}]', _short(expected[index]), '(fehlt im frischen Stand)'
            found = first_difference(expected[index], actual[index], f'{path}[{index}]')
            if found:
                return found
        return None
    if isinstance(expected, str) and isinstance(actual, str) and expected != actual:
        index = next((i for i, (one, other) in enumerate(zip(expected, actual)) if one != other), min(len(expected), len(actual)))
        return path, _window(expected, index), _window(actual, index)
    if expected != actual or type(expected) is not type(actual):
        return path, _short(expected), _short(actual)
    return None


# ═══ Dokumente ════════════════════════════════════════════════════════════════
def _pdf_document(pdf_bytes):
    return PDFDocument(PDFParser(io.BytesIO(pdf_bytes)))


def content_streams(pdf_bytes):
    """Der Content-Stream jeder Seite (entpackt) – das, was die Seite zeichnet, ohne Erzeugungszeit und Kennungen der Datei."""
    return [b''.join(stream_value(part).get_data() for part in page.contents) for page in PDFPage.create_pages(_pdf_document(pdf_bytes))]


def embedded_xml(pdf_bytes):
    """Die ``factur-x.xml``, die das PDF trägt (Bytes), sonst ``None``."""
    catalog = _pdf_document(pdf_bytes).catalog
    if 'AF' not in catalog:
        return None
    return stream_value(resolve1(resolve1(resolve1(catalog['AF'])[0])['EF'])['F']).get_data()


_TEXT = re.compile(rb'1 0 0 1 (-?[0-9.]+) (-?[0-9.]+) Tm|\(((?:\\.|[^\\()])*)\)\s*Tj')
_READABLE = re.compile(r'\d+,\d\d|MwSt|USt|Kleinunternehmer|UStG|Fassung|Storno|Ersetzt|Berichtigt|Gesamt|Summe')
_SIMPLE_ESCAPES = {ord('n'): 10, ord('r'): 13, ord('t'): 9, ord('b'): 8, ord('f'): 12}


def _pdf_string(raw):
    out, index = bytearray(), 0
    while index < len(raw):
        char = raw[index]
        if char == 0x5C and index + 1 < len(raw):
            following = raw[index + 1]
            if 0x30 <= following <= 0x37:
                digits = bytearray()
                index += 1
                while index < len(raw) and len(digits) < 3 and 0x30 <= raw[index] <= 0x37:
                    digits.append(raw[index])
                    index += 1
                out.append(int(digits, 8) & 0xFF)
                continue
            out.append(_SIMPLE_ESCAPES.get(following, following))
            index += 2
            continue
        out.append(char)
        index += 1
    return out.decode('cp1252', errors='replace')


def readable_lines(streams):
    """Die Zeilen der Seiten, die Beträge, Steuer, Summen oder einen Hinweis tragen – gelesen aus den Textoperatoren des Content-Streams
    (Texte mit gleicher Höhe zu einer Zeile, von links nach rechts), von oben nach unten. Nur eine Lesehilfe: Den Beweis führt der Hash."""
    result = []
    for page, stream in enumerate(streams, start=1):
        rows, x, y = defaultdict(list), 0.0, 0.0
        for match in _TEXT.finditer(stream):
            if match.group(1) is not None:
                x, y = float(match.group(1)), float(match.group(2))
            else:
                rows[y].append((x, _pdf_string(match.group(3))))
        for height in sorted(rows, reverse=True):
            text = re.sub(r'\s+', ' ', ' '.join(part for _x, part in sorted(rows[height], key=lambda item: item[0]))).strip()
            if _READABLE.search(text):
                result.append(f'S{page} {text}')
    return result


def _local(element):
    return element.tag.rsplit('}', 1)[-1]


def _children(element):
    return {_local(child): (child.text or '').strip() for child in element if len(child) == 0}


def _find_all(root, name):
    return [element for element in root.iter() if _local(element) == name]


def cii_facts(xml_bytes):
    """Die lesbaren Kernwerte einer CII: Nummer und Art, Hinweise (BT-22), Steueraufschlüsselung (Kategorie, Satz, Basis, Steuer),
    Summen, Zahlungsbedingungen."""
    root = ET.fromstring(xml_bytes)
    header = next(element for element in _find_all(root, 'ExchangedDocument'))
    settle = next(element for element in _find_all(root, 'ApplicableHeaderTradeSettlement'))
    taxes = [_children(tax) for tax in settle if _local(tax) == 'ApplicableTradeTax']
    sums = next(element for element in settle if _local(element) == 'SpecifiedTradeSettlementHeaderMonetarySummation')
    terms = [_children(element) for element in _find_all(settle, 'SpecifiedTradePaymentTerms')]
    return {'number': _children(header).get('ID'), 'type': _children(header).get('TypeCode'),
            'notes': [element.text for element in _find_all(header, 'Content')],
            'tax': [{'category': t.get('CategoryCode'), 'rate': t.get('RateApplicablePercent'), 'basis': t.get('BasisAmount'),
                     'amount': t.get('CalculatedAmount'), 'reason': t.get('ExemptionReason')} for t in taxes],
            'totals': _children(sums), 'terms': terms}


def ubl_facts(xml_bytes):
    """Die lesbaren Kernwerte eines UBL: Wurzel, Nummer, Hinweise, Steueraufschlüsselung, Summen, Zahlungsbedingungen."""
    root = ET.fromstring(xml_bytes)
    top = _children(root)
    subtotals = []
    for subtotal in _find_all(root, 'TaxSubtotal'):
        category = next(element for element in subtotal if _local(element) == 'TaxCategory')
        subtotals.append({'basis': _children(subtotal).get('TaxableAmount'), 'amount': _children(subtotal).get('TaxAmount'),
                          **{key: value for key, value in _children(category).items() if key in ('ID', 'Percent', 'TaxExemptionReason')}})
    totals = next(element for element in root.iter() if _local(element) == 'LegalMonetaryTotal')
    return {'root': _local(root), 'number': top.get('ID'), 'notes': [element.text for element in root if _local(element) == 'Note'],
            'tax': subtotals, 'totals': _children(totals),
            'terms': [_children(element) for element in root if _local(element) == 'PaymentTerms']}


def pdf_facts(pdf_bytes):
    """Seitenzahl, Hash der Content-Streams und lesbare Zeilen einer PDF."""
    streams = content_streams(pdf_bytes)
    digest = hashlib.sha256(b'\n-- Seite --\n'.join(streams)).hexdigest()
    return {'pages': len(streams), 'sha256': digest, 'lines': readable_lines(streams)}


def document_facts(db, invoice_id):
    """Was eine Rechnung an Dokumenten abgelegt hat: ihre PDF (Content-Streams), die eingebettete CII und – falls es sie gibt – die UBL-Datei."""
    with contextlib.closing(sqlite3.connect(db.db_name)) as con:
        pdf_path, xml_path = con.execute('SELECT PDFPath, XMLPath FROM Invoices WHERE ID = ?', (invoice_id,)).fetchone()
    with open(pdf_path, 'rb') as handle:
        pdf = handle.read()
    facts = {'pdf': pdf_facts(pdf)}
    cii = embedded_xml(pdf)
    facts['cii'] = {'sha256': hashlib.sha256(cii).hexdigest(), **cii_facts(cii)} if cii else None
    if xml_path:
        with open(xml_path, 'rb') as handle:
            ubl = handle.read()
        facts['ubl'] = {'sha256': hashlib.sha256(ubl).hexdigest(), **ubl_facts(ubl)}
    else:
        facts['ubl'] = None
    return facts


# ═══ der Ablauf ═══════════════════════════════════════════════════════════════
class Scenario:
    """Der Ablauf eines Steuersatzes in einem frischen Bestand (``db``). ``run()`` führt alle Abläufe aus, ``snapshot()`` liefert danach, was
    festzuhalten ist. Jeder Ablauf hat eigene Rechnungen, damit sich die Abläufe nicht stören; die Nummern vergibt der Nummernkreis."""

    def __init__(self, db, case):
        self.db, self.case = db, case
        self.calls = []              # [[Schritt, Ergebnis], …] – die Meldungen der Aufrufe in der Reihenfolge, in der sie liefen
        self.documents = {}
        self.chosen_revenue_coa = None   # das Erlöskonto, das der Nutzer bei einem Satz ohne hinterlegtes Konto wählt (4400)
        self.own = self.customer = self.bank = None

    # ── Aufbau ───────────────────────────────────────────────────────────────
    def build_world(self):
        """Eigene Firma, ein Kunde, ein Bankkonto (SKR 1800) und der Rechnungskreis 2025 – Namen und Nummern wie in ``tests/invoice_world.py``."""
        db = self.db
        self.own = db.insert_contact(
            contact_type='own', entity_type='company', display_name='Eigene Firma GmbH', company_name='Eigene Firma GmbH',
            email='info@example.com', street='Hauptstr. 1', postal_code='10115', city='Berlin', phone='030 1234567',
            tax_id='DE123456789')
        self.customer = db.insert_contact(
            contact_type='customer', entity_type='company', display_name='Malermeister Pinsel', company_name='Malermeister Pinsel',
            email='info@example.com', customer_number='K-100', street='Kundenweg 5', postal_code='54321', city='Kundenhausen')
        db.insert_account('Giro', 'Eigene Firma GmbH', 'DE89370400440532013000', 'TESTDE1X', 'Testbank AG', is_cash=0,
                          skr_account=1800)
        self.bank = next(account[0] for account in db.fetch_accounts() if account[1] == 'Giro')
        db.insert_number_range('invoice', 2025, 'R', current_number=0)
        self.chosen_revenue_coa = db.get_coa_id_by_account_number(4400)

    def items(self):
        rate = self.case.item_rate
        return [{'position': 1, 'quantity': 1, 'unit': 'C62', 'description': 'Beratung', 'unitPrice': 100.0, 'taxRate': rate},
                {'position': 2, 'quantity': 3, 'unit': 'C62', 'description': 'Material', 'unitPrice': 33.33, 'taxRate': rate}]

    def payload(self, date, **over):
        """Das Formular einer Rechnung, wie die Maske es schickt (``pages_invoice.py``: Satz je Position in Prozent, im Kopf als Bruch,
        ``showTax``); die Nummer ist der Vorschlag (``numberSuggestion``), also vergibt der Server sie frisch."""
        day = datetime.date.fromisoformat(date)
        payload = {
            'invoiceNumber': 'Vorschlag', 'numberSuggestion': 'Vorschlag', 'invoiceDate': date, 'deliveryDate': date,
            'dueDate': (day + datetime.timedelta(days=14)).isoformat(), 'customerId': self.customer, 'ownCompanyId': self.own,
            'bankAccountId': self.bank, 'taxRate': self.case.head_rate, 'showTax': self.case.show_tax,
            'paymentTerms': 'Zahlbar in 14 Tagen', 'orderNumber': 'B-17', 'buyerReference': LEITWEG_ID, 'currency': 'EUR',
            'items': self.items()}
        payload.update(over)
        return payload

    def invoice(self, step, date, **over):
        """Eine Rechnung als Entwurf speichern (``handlers.handle_invoice_save``); liefert ihre ID."""
        body = json.loads(handlers.handle_invoice_save(json.dumps(self.payload(date, **over)).encode()))
        self.record(step, body)
        assert body['success'], body
        return body['invoice_id']

    def gross(self, invoice_id):
        return self.db.get_invoice_by_id(invoice_id)[38]

    def bank_booking(self, date, amount):
        return self.db.insert_booking(date, Decimal(amount), account_id=self.bank, booking_type='bank',
                                      recipient_client='Malermeister Pinsel')

    def record(self, step, value):
        self.calls.append([step, plain(value)])

    # ── Wege der App ─────────────────────────────────────────────────────────
    def status(self, step, invoice_id, new_status):
        """``Database.set_invoice_status`` (ohne Dokument) – der Statuswechsel der Route, ohne das Erzeugen des PDFs."""
        change = self.db.set_invoice_status(invoice_id, new_status, USER)
        self.record(step, change)
        return change

    def status_route(self, step, invoice_id, new_status):
        """``handlers.handle_update_invoice_status`` – wie die Route, samt PDF (+ UBL) beim Versenden."""
        code, text = handlers.handle_update_invoice_status(json.dumps({'invoice_id': invoice_id, 'status': new_status}).encode())
        body = json.loads(text)
        self.record(step, [code, body])
        return code, body

    def link(self, step, invoice_id, bank_id, amount=None, revenue_coa_id=None):
        """``handlers.handle_link_invoice_payment`` (Zuordnen einer Zahlung bzw. – an einer Stornorechnung – einer Erstattung)."""
        data = {'invoice_id': invoice_id, 'transaction_id': bank_id, 'amount_paid': amount}
        if revenue_coa_id is not None:
            data['revenue_coa_id'] = revenue_coa_id
        result = handlers.handle_link_invoice_payment(json.dumps(data).encode())
        self.record(step, list(result))
        return result

    def pay(self, step, invoice_id, bank_id, amount=None):
        """Zahlung (oder Erstattung) zuordnen; verlangt die App das Erlöskonto, weil für den Satz keines hinterlegt ist (5 %), wird die
        Ablehnung festgehalten und die Zuordnung mit der Wahl wiederholt."""
        result = self.link(step, invoice_id, bank_id, amount)
        if result[0] == 400 and 'Erlöskonto' in result[1]:
            result = self.link(f'{step} – mit gewähltem Erlöskonto', invoice_id, bank_id, amount, self.chosen_revenue_coa)
        return result

    def unpay(self, step, payment_id):
        result = handlers.handle_delete_invoice_payment(json.dumps({'payment_id': payment_id}).encode())
        self.record(step, list(result))
        return result

    def payment_ids(self, invoice_id):
        return [row[0] for row in self.db.get_invoice_payments(invoice_id)]

    def cancel(self, step, invoice_id, date):
        """``Database.cancel_invoice`` (ohne Dokument der Stornorechnung): Stornorechnung, Status, Gegenbuchung."""
        storno = self.db.cancel_invoice(invoice_id, date, USER)
        self.record(step, storno)
        assert storno.ok, storno.message
        return storno.storno_id

    def reissue(self, step, invoice_id):
        replacement = self.db.create_replacement_draft(invoice_id, USER)
        self.record(step, replacement)
        assert replacement.ok, replacement.message
        return replacement.invoice_id

    def rebuild(self, step, invoice_id):
        """Der Probelauf von „Zuordnung neu aufbauen“ über den Handler der Route (``handlers.handle_invoice_rebuild_payments``, ohne
        ``execute``): der Plan samt Prüfsumme, so wie die Seite ihn bekommt. Es wird nichts geschrieben."""
        code, text = handlers.handle_invoice_rebuild_payments(self.db, json.dumps({'invoiceId': invoice_id}).encode())
        self.record(step, [code, json.loads(text)])

    def findings(self, invoice_id=None):
        return [[f['category'], f['invoice_number'], f['text']] for f in self.db.find_payment_inconsistencies(invoice_id)]

    def keep_documents(self, name, invoice_id):
        self.documents[name] = document_facts(self.db, invoice_id)

    # ── die Abläufe ──────────────────────────────────────────────────────────
    def run(self):
        db = self.db
        self.build_world()

        # 1. Versenden (Entwurf → versendet) mit dem Dokument, das der Kunde bekommt: PDF mit eingebetteter CII und UBL. Skonto und
        #    Leitweg-ID, damit nichts fehlt (``einvoice_gaps`` leer)
        a = self.invoice('1 Rechnung anlegen', '2025-03-03', discountPercentage=2, discountDays=7)
        self.status_route('1 Versenden (Route, mit PDF und UBL)', a, 'sent')
        self.keep_documents('invoice', a)

        # 2. Teilzahlung 50,00, danach die schließende Zahlung
        b = self.invoice('2 Rechnung anlegen', '2025-03-04')
        self.status('2 Versenden', b, 'sent')
        part = self.bank_booking('2025-04-02', '50.00')
        self.pay('2 Teilzahlung 50,00', b, part, 50.0)
        rest = self.bank_booking('2025-04-04', str(self.gross(b) - Decimal('50.00')))
        self.pay('2 Schließende Zahlung', b, rest)

        # 3. Sammelzahlung: EINE Bankbewegung begleicht zwei Rechnungen
        c1 = self.invoice('3 Rechnung 1 anlegen', '2025-03-05')
        c2 = self.invoice('3 Rechnung 2 anlegen', '2025-03-06', items=self.items()[:1])
        self.status('3 Versenden 1', c1, 'sent')
        self.status('3 Versenden 2', c2, 'sent')
        collective = self.bank_booking('2025-04-07', str(self.gross(c1) + self.gross(c2)))
        self.pay('3 Zahlung Rechnung 1', c1, collective)
        self.pay('3 Zahlung Rechnung 2', c2, collective)

        # 4. Überzahlung: die Bankbewegung ist 20,00 größer als der offene Betrag; der Rest bleibt frei und wird danach einer zweiten
        #    Rechnung zugeordnet (sonst wäre die Bewegung ein unstimmiger Split und der DATEV-Export gesperrt)
        e = self.invoice('4 Rechnung anlegen', '2025-03-07')
        e2 = self.invoice('4 zweite Rechnung anlegen', '2025-03-08')
        self.status('4 Versenden', e, 'sent')
        self.status('4 Versenden (zweite)', e2, 'sent')
        over = self.bank_booking('2025-04-08', str(self.gross(e) + Decimal('20.00')))
        self.pay('4 Überzahlung: so viel wie möglich', e, over)
        self.record('4 Freier Rest der Bewegung', db.get_booking_unallocated_amount(over))
        self.pay('4 Der Rest der Bewegung (20,00) an die zweite Rechnung', e2, over, 20.0)
        self.pay('4 Dieselbe Bewegung noch einmal an dieselbe Rechnung', e, over)

        # 5. Zahlung zuordnen und wieder entfernen: eine volle Zahlung bei vergangener Fälligkeit (danach „überfällig“) und eine
        #    Teilzahlung bei Fälligkeit in der Zukunft (danach „versendet“)
        f = self.invoice('5 Rechnung anlegen (fällig im März)', '2025-03-10')
        f2 = self.invoice('5 Rechnung anlegen (fällig im Dezember)', '2025-03-11', dueDate='2025-12-31')
        self.status('5 Versenden', f, 'sent')
        self.status('5 Versenden (zweite)', f2, 'sent')
        self.pay('5 Zahlung', f, self.bank_booking('2025-04-09', str(self.gross(f))))
        self.pay('5 Teilzahlung (zweite)', f2, self.bank_booking('2025-04-10', '50.00'), 50.0)
        for number, invoice_id in (('', f), (' (zweite)', f2)):
            for payment_id in self.payment_ids(invoice_id):
                self.unpay(f'5 Zahlung entfernen{number}', payment_id)

        # 6. Stornieren einer teilbezahlten Rechnung (Guthaben), danach Teilerstattung und Rest-Erstattung über Zahlungsausgänge
        g = self.invoice('6 Rechnung anlegen', '2025-03-12')
        self.status('6 Versenden', g, 'sent')
        self.pay('6 Teilzahlung 100,00', g, self.bank_booking('2025-04-11', '100.00'), 100.0)
        storno = self.cancel('6 Stornieren', g, '2025-05-12')
        self.record('6 Guthaben nach dem Stornieren', db.get_storno_credit(g))
        self.pay('6 Teilerstattung 40,00', storno, self.bank_booking('2025-05-15', '-40.00'), 40.0)
        self.record('6 Guthaben nach der Teilerstattung', db.get_storno_credit(g))
        self.pay('6 Rest-Erstattung', storno, self.bank_booking('2025-05-16', '-60.00'))
        self.record('6 Guthaben und Erstattetes nach der Rest-Erstattung', [db.get_storno_credit(g), db.get_storno_refunded(g)])

        # 7a. Stornieren und neu ausstellen (Route: Stornorechnung mit PDF und UBL, Entwurf der neuen Rechnung), die Ersatzrechnung
        #     versenden (Route): die volle Zahlung zieht um
        h = self.invoice('7a Rechnung anlegen', '2025-03-13')
        self.status('7a Versenden', h, 'sent')
        self.pay('7a Zahlung', h, self.bank_booking('2025-04-14', str(self.gross(h))))
        code, text = handlers.handle_invoice_cancel(json.dumps({'invoiceId': h, 'date': '2025-05-20', 'reissue': True}).encode())
        body = json.loads(text)
        self.record('7a Stornieren und neu ausstellen (Route)', [code, body])
        self.keep_documents('storno', body['storno_id'])
        self.status_route('7a Ersatzrechnung versenden (Route, Umzug der Zahlung)', body['replacement_id'], 'sent')
        self.keep_documents('replacement', body['replacement_id'])

        # 7b. „50,00 gezahlt, 20,00 erstattet, Ersatzrechnung über den vollen Betrag“
        i = self.invoice('7b Rechnung anlegen', '2025-03-14')
        self.status('7b Versenden', i, 'sent')
        self.pay('7b Zahlung 50,00', i, self.bank_booking('2025-04-15', '50.00'), 50.0)
        storno_i = self.cancel('7b Stornieren', i, '2025-05-21')
        self.pay('7b Erstattung 20,00', storno_i, self.bank_booking('2025-05-22', '-20.00'), 20.0)
        self.record('7b Guthaben nach der Erstattung', db.get_storno_credit(i))
        replacement_i = self.reissue('7b Neu ausstellen', i)
        self.status('7b Ersatzrechnung versenden (Umzug des Guthabens)', replacement_i, 'sent')

        # 8. Zurückholen einer versendeten Rechnung ohne Zahlung (versendet → Entwurf) und erneut versenden – und der Versuch an einer
        #    Rechnung mit Zahlung, den die App ablehnt
        j = self.invoice('8 Rechnung anlegen', '2025-03-17')
        self.status('8 Versenden', j, 'sent')
        self.status('8 Zurückholen', j, 'draft')
        self.status('8 Erneut versenden', j, 'sent')
        j2 = self.invoice('8 Rechnung mit Zahlung anlegen', '2025-03-17')
        self.status('8 Versenden (mit Zahlung)', j2, 'sent')
        self.pay('8 Teilzahlung 50,00', j2, self.bank_booking('2025-04-17', '50.00'), 50.0)
        self.status('8 Zurückholen mit Zahlung (abgelehnt)', j2, 'draft')

        # 9. Berichtigen einer versendeten Rechnung (Zahlungshinweis), mit dem Dokument der neuen Fassung
        k = self.invoice('9 Rechnung anlegen', '2025-03-18')
        self.status_route('9 Versenden (Route, mit PDF und UBL)', k, 'sent')
        with self._pinned_protocol():
            body = json.loads(handlers.handle_invoice_save(json.dumps(
                {'invoiceId': k, 'documentVersion': 1, 'paymentTerms': 'Zahlbar innerhalb von 30 Tagen ohne Abzug'}).encode()))
        self.record('9 Berichtigen (Zahlungshinweis)', body)
        self.keep_documents('corrected', k)

        # 10. Probelauf von „Zuordnung neu aufbauen“: an einer Rechnung, deren Zuordnung keine Buchungssätze hat (so ordnet der alte
        #     Verknüpfungsweg zu – ob die Prüfliste und der Neuaufbau das bemerken, hängt vom Satz ab: ohne Forderung, bei 5 %, nicht),
        #     und an einer, deren Zuordnung ihre Sätze hat
        lost = self.invoice('10 Rechnung anlegen', '2025-03-19')
        self.status('10 Versenden', lost, 'sent')
        db.link_invoice_to_transaction(lost, self.bank_booking('2025-04-16', str(self.gross(lost))), self.gross(lost))
        self.record('10 Prüfliste für die Rechnung ohne Sätze', self.findings(lost))
        self.rebuild('10 Probelauf: Zuordnung ohne Buchungssätze', lost)
        self.rebuild('10 Probelauf: Zuordnung mit Buchungssätzen', b)

        # Ein Angebot mit demselben Satz (nur PDF); danach wird es in eine Rechnung umgewandelt (Entwurf, Satz und Positionen aus dem Angebot)
        quote = self.quote()
        pdf, _path = generate_quote_pdf(db, quote)
        self.documents['quote'] = {'pdf': pdf_facts(pdf)}
        self.record('11 Angebot in Rechnung umwandeln', list(handlers.handle_convert_quote_to_invoice({'quote_id': [str(quote)]})))

    def quote(self):
        """Ein Angebot mit demselben Satz speichern, wie die Maske es schickt (``handlers.handle_quote_save``); liefert seine ID."""
        payload = {'quoteNumber': 'A25-001', 'quoteDate': '2025-02-20', 'validUntil': '2025-03-20', 'customerId': self.customer,
                   'ownCompanyId': self.own, 'taxRate': self.case.head_rate, 'showTax': self.case.show_tax,
                   'introText': '<b>Guten Tag</b>', 'closingText': 'Mit freundlichen Grüßen', 'items': self.items()}
        body = json.loads(handlers.handle_quote_save(json.dumps(payload).encode()))
        self.record('Angebot anlegen', body)
        assert body['success'], body
        return body['quote_id']

    @contextlib.contextmanager
    def _pinned_protocol(self):
        """Solange die Berichtigung läuft: Bevor ``handlers`` das Dokument der neuen Fassung erzeugt, bekommen die Protokollzeilen feste
        Zeitpunkte (siehe Modul-Docstring) – der Versand am 18.03., die Berichtigung am 20.05.2025."""
        original = handlers._generate_corrected_documents

        def pinned(db, invoice_id):
            with contextlib.closing(sqlite3.connect(db.db_name)) as con:
                con.execute("UPDATE InvoiceCorrections SET ChangedAt = CASE Action WHEN 'correct' THEN '2025-05-20 10:00:00' "
                            "ELSE '2025-03-18 10:00:00' END WHERE InvoiceID = ?", (invoice_id,))
                con.execute("UPDATE Invoices SET SentAt = '2025-03-18 10:00:00' WHERE ID = ?", (invoice_id,))
                con.commit()
            return original(db, invoice_id)

        with mock.patch.object(handlers, '_generate_corrected_documents', pinned):
            yield

    # ── festhalten ───────────────────────────────────────────────────────────
    def snapshot(self):
        """Der Stand nach allen Abläufen. Der DATEV-Export kommt zuletzt: Er stempelt das Steuerdatum der exportierten Sätze."""
        db = self.db
        euer = [[number, name, plain(total)] for number, name, total in db.get_euer_data(*YEAR)]
        susa = {f'{year}-{month:02d}': plain(db.get_susa(year, month)) for year, month in SUSA_MONTHS}
        dashboard = plain(db.get_dashboard(*YEAR))
        findings = self.findings()
        snapshot = {
            'calls': self.calls,
            'bookings': table_lines(db, 'Bookings'),
            'payments': table_lines(db, 'InvoicePayments', exclude=('CreatedAt',)),
            'invoices': table_lines(db, 'Invoices', only=(
                'ID', 'InvoiceNumber', 'InvoiceDate', 'PaymentDueDate', 'DocumentType', 'InvoiceKind', 'Status', 'TaxCategory', 'TaxRate',
                'SumNet', 'TaxAmount', 'SumGross', 'AmountDue', 'SkontoDays', 'SkontoPercent', 'CancelsInvoiceId', 'ReplacesInvoiceId',
                'DocumentVersion')),
            'items': table_lines(db, 'InvoiceItems'),
            # das Protokoll der Statuswechsel und Berichtigungen (ohne Zeitpunkt) und die abgelegten Fassungen (ohne Pfad, Hash und Zeitpunkt)
            'protocol': table_lines(db, 'InvoiceCorrections', exclude=('ChangedAt',)),
            'versions': table_lines(db, 'InvoiceDocumentVersions', only=('InvoiceID', 'Version', 'Kind', 'Reason'), order='ID'),
            'findings': findings, 'open_items': plain(db.get_open_items_for_contact(self.customer)),
            'euer': euer, 'susa': susa, 'dashboard': dashboard, 'documents': self.documents}
        snapshot['datev'] = self.export_datev()
        snapshot['date_tax_after_export'] = table_lines(db, 'Bookings', only=('ID', 'DateTax'), where='WHERE DateTax IS NOT NULL')
        return snapshot

    def export_datev(self):
        """Der Stapel des Jahres, wie der Nutzer ihn bestätigt: erst ohne Bestätigung (gibt es Hinweise – Bankbewegungen ohne Buchungssatz –,
        nennt die Weiterleitung sie), dann mit dem Kästchen für genau diesen Hinweis. Festgehalten: der Hinweis, der Hash der Bytes, der Vorlauf
        und je Zeile die Felder, die etwas enthalten (Spaltennummer und Wert). Wird der Export abgelehnt, steht die Ablehnung da."""
        first = handlers.handle_datev_export(self.db, datev_post(*YEAR))
        confirmation, result = None, first
        if is_confirm_redirect(first):
            confirmation = message_of(first)
            result = handlers.handle_datev_export(self.db, datev_post(*YEAR, confirm_export='1', confirmed_check=check_of(first)))
        if not isinstance(result[0], bytes):
            return {'confirmation': confirmation, 'refused': plain(list(result))}
        lines = [line for line in result[0].decode('cp1252').split('\r\n') if line]
        rows = []
        for line in lines[2:]:
            fields = next(csv.reader([line], delimiter=';', quotechar='"'))
            rows.append(' | '.join(f'{index}={value}' for index, value in enumerate(fields) if value))
        return {'confirmation': confirmation, 'filename': result[1], 'sha256': hashlib.sha256(result[0]).hexdigest(), 'bytes': len(result[0]),
                'header': lines[0], 'rows': rows}


@contextlib.contextmanager
def environment(db):
    """Die Umgebung eines Ablaufs: die Handler arbeiten auf ``db``, der angemeldete Benutzer ist ``USER`` (Dokumente liegen unter
    ``<Datenordner>/users/<USER>/``), die Uhr steht fest."""
    userctx.set_user(USER)
    try:
        with mock.patch.object(handlers, 'Database', lambda *args, **kwargs: db), frozen_clock():
            yield
    finally:
        userctx.clear()


def build_snapshot(db, key):
    """Den Ablauf des Satzes ``key`` in der frischen Datenbank ``db`` ausführen und den Stand festhalten."""
    with environment(db):
        scenario = Scenario(db, CASES[key])
        scenario.run()
        return scenario.snapshot()
