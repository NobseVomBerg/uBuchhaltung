# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Inventar der Lesestellen des Belegsteuersatzes (statisch, ``ast``).

Ein Beleg (Rechnung, Angebot) trägt heute EINEN Steuersatz im Kopf: ``Invoices.TaxRate``, Spalte 35 der Zeile (``SELECT *``) – ein Bruch wie 0,19,
eine Prozentzahl, 0 oder der Platzhalter −1 für „kein Steuerausweis“ (§ 19 UStG). Mit Belegen, deren Positionen verschiedene Sätze tragen (Plan
2.7.0), sagt dieser Satz nicht mehr, was der Beleg bucht, ausgibt und anzeigt; maßgeblich sind dann die Satzgruppen (``tax_groups.rate_groups``). Damit
keine Stelle still den Kopfsatz für den ganzen Beleg nimmt, führt dieser Test jede Stelle auf, die ihn liest.

**Gesucht** wird in ``db/``, ``server/``, ``export/``, ``importers/``, ``tools/`` und in ``demo.py``, ``demo_seed.py``, ``demo_history.py`` und
``tax_groups.py`` (im Syntaxbaum, nicht zeilenweise: Kommentare und Docstrings zählen nicht):

* ``x[35]`` – die Spalte 35 der Belegzeile, gleich wie die Variable heißt (``invoice[35]``, ``quote[35]``, ``existing[35]`` …);
* ``InvoiceCol.TAX_RATE`` – falls die Spalte dort einen Namen bekommt;
* eine Zeichenkette, die nur die Spalte nennt: ``'TaxRate'`` (``row['TaxRate']``, ``.get('TaxRate')``, die Feldtabelle der Sperre) oder
  ``'i.TaxRate'`` (die Spaltenangabe, die ein Aufrufer in eine SQL-Abfrage einsetzt);
* ein SQL-Text, der ``TaxRate`` und die Tabelle ``Invoices`` nennt (``SELECT … TaxRate … FROM Invoices``, ``INSERT INTO Invoices``,
  ``UPDATE Invoices``, ``CREATE TABLE Invoices``);
* ``col('tax_rate')`` und ``_INV['tax_rate']`` – die Spaltentabelle des E-Rechnungsmodells (``export/einvoice_model.py``).

Nicht gesucht werden die Sätze der Positionen (``InvoiceItems.TaxRate``) und der Wert, den die Maske beim Speichern schickt (``taxRate`` im
Formular) – beides gehört zum Schreiben eines Belegs, nicht zum Lesen seines Kopfsatzes.

Verglichen wird die Menge ``(Datei, umschließende Funktion)`` mit zwei Listen: ``READERS`` – Stellen, die den Satz EINES BELEGS lesen (oder, wo es
dabeisteht, schreiben), je mit einer Kurzbeschreibung – und ``NOT_THE_DOCUMENT_RATE`` – Treffer, die nachweislich etwas anderes meinen
(``Bookings.TaxRate``, ``InvoiceItems.TaxRate``, die Lesung einer Belegdatei), je mit der Begründung. Die umschließende Funktion ist die äußerste (eine
Hilfsfunktion in einer Funktion zählt zu dieser); ohne Funktion steht ``<class Name>`` bzw. ``<module>``. Eine neue Stelle bricht den Test, eine
verschwundene auch: Die Listen sollen genau das beschreiben, was im Code steht. Lieber ein Treffer zu viel als eine übersehene Stelle.
"""
import ast
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCANNED_DIRECTORIES = ('db', 'server', 'export', 'importers', 'tools')
SCANNED_FILES = ('demo.py', 'demo_seed.py', 'demo_history.py', 'tax_groups.py')

# Stellen, die den Steuersatz eines BELEGS lesen (oder, wo es dabeisteht, schreiben): (Datei, Funktion) → was dort damit geschieht
READERS = {
    ('db/invoice_lock.py', '<module>'): ('Sperre: Feldtabelle FIELDS (der Satz ist eine gesperrte Eingabe der versendeten Rechnung; dazu das Kennzeichen '
                                         '„Steuersatz je Position“, MixedTaxRates, als abgeleitetes, gesperrtes Feld)'),
    ('db/invoice_lock.py', '_payment_move_context'): ('Umzug der Zahlungen auf die Ersatzrechnung: Satz und Erlöskonto der neuen Rechnung; bei einem Beleg mit '
                                                      'Steuersatz je Position (MixedTaxRates) und bei einem Erlöskonto aus den Stammdaten (Momentaufnahme eines '
                                                      'Steuersatzes, an einer der beiden Rechnungen) nie ein Erlöskonto (der Kopfsatz ist nur ein Platzhalter bzw. das '
                                                      'Konto gilt), es zieht nichts um'),
    ('db/invoice_lock.py', '_receivable_accounts_for'): ('Forderung buchen: Wartekonto und Schlüssel des Satzes (Statuswechsel); ein Beleg mit '
                                                         'Steuersatz je Position (MixedTaxRates) nimmt den Kopfsatz nie: Die Konten gelten für seine Satzgruppen '
                                                         '(_mixed_receivable_accounts), der Schlüssel hält sie samt den Momentaufnahmen der Stammdaten fest; ein Beleg '
                                                         'mit einem Satz und Momentaufnahme bekommt das Wartekonto genau seines Satzes (_entry_receivable_accounts, '
                                                         'nicht _tax_rate_key)'),
    ('db/invoice_lock.py', 'set_invoice_status'): ('Statuswechsel: Satz unverändert seit der Vorprüfung, Umzug der Zahlungen nur bei gleichem Erlöskonto; '
                                                   'bei einem Beleg mit Steuersatz je Position (MixedTaxRates) zählt statt des Satzes der Stand seiner '
                                                   'Satzgruppen und ihrer Momentaufnahmen (unverändert seit der Vorprüfung, sonst abgelehnt); bei einer Momentaufnahme '
                                                   'der Stammdaten (auch mit einem Satz) gehört sie zum Schlüssel'),
    ('db/invoice_payments.py', '_assign_in_tx'): ('Zahlung zuordnen: Satz und Steuer der Rechnung für die Buchungssätze der Zahlung; ein Beleg mit '
                                                  'Steuersatz je Position (MixedTaxRates) bucht je Satz (_assign_mixed) – sein Kopfsatz dient nur dem Vergleich '
                                                  'mit dem Kontext, Kennzeichen und Kontext müssen zusammenpassen (sonst abgelehnt)'),
    ('db/invoice_payments.py', '_move_replaced_payments'): ('Umzug der Zahlungen: Satz der stornierten Rechnung gegen den der neuen, Neubuchung; mit '
                                                            'Steuersatz je Position (MixedTaxRates) oder einem Erlöskonto aus den Stammdaten an einer der beiden zieht '
                                                            'nichts um'),
    ('db/invoice_payments.py', '_payment_prerequisites'): ('Zahlung zuordnen: Erlöskonto des Satzes und Satz im Kontext; ein Beleg mit Steuersatz je '
                                                           'Position (MixedTaxRates) hat je Satzgruppe ein Erlöskonto und keine Wahl (der Kopfsatz steht im '
                                                           'Kontext nur als Platzhalter); trägt die Gruppe bzw. der Beleg ein Erlöskonto der Stammdaten '
                                                           '(Momentaufnahme), gilt es, immer'),
    ('db/invoice_payments.py', '_rate_findings'): ('Prüfliste: Der Kopf unter 0 („ohne Steuerausweis“) schließt „Positionen mit verschiedenen Sätzen ohne das '
                                                   'Kennzeichen“ aus – sonst liest die Prüfung nur die Sätze der Positionen (bei einer Momentaufnahme ihren Satz in '
                                                   'Hundertstel Prozent) und der Buchungssätze, nie den Kopfsatz als Satz des Belegs'),
    ('db/invoice_payments.py', '_tax_entry_findings'): ('Prüfliste (tax_entry_mismatch): Bei einem Beleg mit EINEM Satz (ohne das Kennzeichen MixedTaxRates) muss der '
                                                        'Kopfsatz der Satz der Momentaufnahme der Stammdaten sein – sonst ein Befund; ein Beleg mit Steuersatz je Position '
                                                        'wird nie gegen den Kopf geprüft (sein Kopfsatz ist nur ein Platzhalter)'),
    ('db/invoice_payments.py', '_refund_plan'): ('Erstattung einer Stornorechnung: Satz und Steuer der stornierten Rechnung; mit Steuersatz je Position '
                                                 '(MixedTaxRates) an beiden Belegen erstattet sie je Satz (_mixed_refund_parts), der Kopfsatz wird nie gelesen; '
                                                 'trägt nur einer von beiden das Kennzeichen, wird abgelehnt'),
    ('db/invoice_payments.py', '_skonto_state'): ('Zahlung mit Skonto (assign_invoice_payment(…, skonto=True)): Kopfsatz der Rechnung für das Skonto-Konto und die Steuer des '
                                                  'Skontos (skonto_rate_key: nur 19 %, 7 %, 0 % und § 19 haben eines, jeder andere Kopfsatz lehnt ab); ein Beleg mit Steuersatz '
                                                  'je Position (MixedTaxRates) oder mit einem Satz aus den Stammdaten lehnt ab – sein Kopfsatz wird nie als Satz eines Skontos '
                                                  'gelesen. Dieselben Regeln in der Vorprüfung und, am frisch gelesenen Stand, in der Transaktion'),
    ('db/invoice_repair.py', '_rebuild_in'): ('Zuordnung neu aufbauen: Satz der Rechnung für die neuen Sätze; ein Beleg mit Steuersatz je Position '
                                              '(MixedTaxRates) baut nichts neu auf (Grund mixed: nur der offene Betrag wird neu gerechnet), ebenso einer mit einem '
                                              'Erlöskonto aus den Stammdaten (Grund tax_entry)'),
    ('db/invoice_repair.py', '_rebuild_prerequisites'): ('Zuordnung neu aufbauen: Erlöskonto des Satzes; bei einem Beleg mit Steuersatz je Position '
                                                         '(MixedTaxRates) und bei einem Erlöskonto aus den Stammdaten keines (der Kopfsatz ist nur ein Platzhalter bzw. '
                                                         'das Konto gilt, es wird nichts neu aufgebaut)'),
    ('db/invoice_storno.py', '_copied_head'): ('Stornorechnung und Ersatzrechnung: Kopf der Ursprungsrechnung kopieren (Satz, Kategorie und das Kennzeichen '
                                               '„Steuersatz je Position“, MixedTaxRates)'),
    ('db/invoice_storno.py', 'cancel_invoice'): ('Stornieren: Wartekonto und Schlüssel des Satzes für die Gegenbuchung der Forderung; ein Beleg mit '
                                                 'Steuersatz je Position (MixedTaxRates) nimmt die Konten und den Schlüssel seiner Satzgruppen '
                                                 '(_mixed_receivable_accounts), nie den Kopfsatz; ein Beleg mit einem Satz und Momentaufnahme der Stammdaten '
                                                 'bekommt – wie beim Versenden (_send_key, _entry_receivable_accounts) – das Wartekonto genau seines Satzes '
                                                 '(nicht _tax_rate_key, der auf ganze Prozent rundet)'),
    ('db/invoices.py', '_book_receivable'): ('Forderung buchen: Satz und Steuer der Forderungszeile; ein Beleg mit Steuersatz je Position (MixedTaxRates) '
                                             'geht über _book_mixed_receivable (je Satzgruppe eine Zeile), nie mit dem Kopfsatz'),
    ('db/invoices.py', '_learned_revenue_row'): ('Erlöskonto lernen (``_learned_revenue_coa`` liest die ID daraus): Der Satz der Rechnung bestimmt den Schlüssel; bei '
                                                 'einem Beleg mit Steuersatz je Position (MixedTaxRates) der Satz des Buchungssatzes, nie der Kopf (nur ein '
                                                 'Platzhalter); Rechnungen mit einem Erlöskonto aus den Stammdaten (Momentaufnahme) lehren nichts'),
    ('db/invoices.py', 'convert_quote_to_invoice'): ('Angebot in eine Rechnung umwandeln: Kopfsatz des Angebots kopieren, dazu das Kennzeichen '
                                                     '„Steuersatz je Position“ (MixedTaxRates)'),
    ('db/invoices.py', 'ensure_receivable_booking'): ('Forderung buchen (Demodaten, Tests): Wartekonto des Satzes; ein Beleg mit Steuersatz je Position '
                                                      '(MixedTaxRates) bucht je Satzgruppe, sein Kopfsatz wird nie gelesen (Kennzeichen und Gruppen werden '
                                                      'in der Transaktion gegen die Vorprüfung verglichen); ein Beleg mit einem Satz und Momentaufnahme bucht über das '
                                                      'Wartekonto genau seines Satzes (Momentaufnahme ebenfalls verglichen)'),
    ('db/invoices.py', 'insert_invoice'): 'Beleg anlegen: schreibt den Satz im Kopf und das Kennzeichen „Steuersatz je Position“ (MixedTaxRates: nur NULL oder 1)',
    ('db/invoices.py', 'last_document_shows_tax'): '§ 19-Prüfung: Häkchen „Umsatzsteuer ausweisen“ eines neuen Belegs',
    ('db/schema.py', 'initialize_database'): 'Schema: legt die Spalte Invoices.TaxRate (und den Satz je Position) an, liest nichts',
    ('export/einvoice_model.py', 'build_einvoice'): 'E-Rechnung: Standardsatz der Positionen ohne eigenen Satz, § 19 (Kategorie E)',
    ('export/pdf_invoice.py', 'generate_invoice_pdf'): ('PDF der Rechnung: Summenblock mit EINEM Satz, § 19-Hinweis; mit Steuersatz je Position '
                                                        '(MixedTaxRates, per_item_rates) Spalte „USt“ und eine Zeile je Satzgruppe – der Kopfsatz wird dann nicht gedruckt'),
    ('export/pdf_quote.py', 'generate_quote_pdf'): ('PDF des Angebots: Summenblock mit EINEM Satz, § 19-Hinweis; mit Steuersatz je Position '
                                                    '(MixedTaxRates, per_item_rates) Spalte „USt“ und eine Zeile je Satzgruppe – der Kopfsatz wird dann nicht gedruckt'),
    ('server/handlers.py', '_backfill_booking_skr'): ('Bankbewegung nach dem Zuordnen: Anzeige von Satz und Steuer; bei einem Beleg mit Steuersatz je '
                                                      'Position (MixedTaxRates) nur Kunde und Beleg-Nr. (der Kopfsatz ist nur ein Platzhalter)'),
    ('server/handlers.py', '_save_quote_update'): 'Angebot ändern: schreibt den Satz im Kopf und das Kennzeichen „Steuersatz je Position“ (MixedTaxRates)',
    ('server/pages_invoice.py', '_invoice_form_html'): ('Rechnungsmaske: Satz und Häkchen „Umsatzsteuer ausweisen“, Erlöskonto-Wahl, Zuordnung neu aufbauen; '
                                                        'die Erlöskonto-Wahl des Zahlungs- und des Neuaufbau-Kastens gibt es bei einem Beleg mit Steuersatz je '
                                                        'Position (MixedTaxRates) und bei einem Erlöskonto aus den Stammdaten nie'),
    ('server/pages_invoice.py', '_refund_box_html'): ('Kasten Erstattung: Erlöskonto-Wahl – bei einer Stornorechnung mit Steuersatz je Position '
                                                      '(MixedTaxRates) oder mit einem Erlöskonto aus den Stammdaten nie'),
    ('server/pages_quote.py', '_quote_form_html'): 'Angebotsmaske: Satz und Häkchen „Umsatzsteuer ausweisen“',
    ('server/pages_transactions.py', 'PageTransactions'): ('Zahlung verbuchen: Erlöskonto-Wahl der Rechnung; bei einem Beleg mit Steuersatz je Position '
                                                           '(MixedTaxRates) oder einem Erlöskonto aus den Stammdaten keine Wahl'),
    ('tools/make_einvoice_samples.py', '_clone_for_correction'): ('Beispieldokumente: Kopf einer Rechnung kopieren, samt dem Kennzeichen '
                                                                  '„Steuersatz je Position“ (MixedTaxRates: nur NULL oder 1)'),
    ('tools/make_einvoice_samples.py', '_normal_invoice'): ('Beispieldokumente: wählt die jüngste Rechnung mit 19 % im Kopf – nie einen Beleg mit '
                                                            'Steuersatz je Position (MixedTaxRates), dessen Kopfsatz nur ein Platzhalter ist'),
}

# Treffer, die nicht den Satz eines Belegs meinen: (Datei, Funktion) → warum nicht
NOT_THE_DOCUMENT_RATE = {
    ('db/bookings.py', '<class BookingsMixin>'): 'Bookings.TaxRate: Feldtabelle von update_booking_fields (Buchungssatz)',
    ('db/document_readings.py', '<module>'): 'DocumentReadings.TaxRate: Lesung einer Belegdatei (receipt_reading), nicht der Satz einer Rechnung',
    ('db/invoice_payments.py', '<module>'): 'Bookings.TaxRate: Spalten und INSERT der Zahlungssätze (PAYMENT_ROW_COLUMNS)',
    ('db/invoice_payments.py', '_same_payment_row'): 'Bookings.TaxRate: Vergleich eines gespeicherten Zahlungssatzes mit dem erwarteten',
    ('db/invoice_payments.py', 'payment_rows'): ('Bookings.TaxRate des Zahlungssatzes; der Satz der Rechnung kommt als Parameter tax_rate von '
                                                 '_assign_in_tx und den anderen Aufrufern – dort sind die Lesestellen'),
    ('db/invoice_payments.py', 'suggest_revenue_coa'): 'Bookings.TaxRate: Vorauswahl des Erlöskontos aus den Buchungen',
    ('db/invoice_repair.py', '_rebuild_plan'): 'Bookings.TaxRate: die neuen Buchungssätze im Plan von „Zuordnung neu aufbauen“',
    ('db/reporting.py', 'get_susa'): 'Bookings.TaxRate: Gruppierung der Sätze der Summen- und Saldenliste',
    ('server/handlers.py', '<module>'): 'InvoiceItems.TaxRate: Bezeichnung des Positionssatzes in der Fehlermeldung (_NOT_NULL_FIELDS)',
    ('server/receipt_reader.py', '<module>'): 'DocumentReadings.TaxRate: Spaltenzuordnung der Lesung einer Belegdatei, nicht der Satz einer Rechnung',
}

NEW_READER_MESSAGE = (
    'Neue Lesestelle des Belegsteuersatzes: prüfen, ob sie mit einem Beleg mit mehreren Sätzen richtig umgeht (der Satz im Kopf ist dort nur ein '
    'Platzhalter, maßgeblich sind die Satzgruppen, tax_groups.rate_groups), dann hier eintragen – in READERS mit einer Kurzbeschreibung, oder in '
    'NOT_THE_DOCUMENT_RATE mit der Begründung, wenn sie nicht den Satz eines Belegs meint (Buchungssatz, Position, Lesung einer Datei …):')
GONE_READER_MESSAGE = (
    'Diese Stelle der Liste liest den Belegsteuersatz nicht mehr (oder heißt anders): Eintrag entfernen bzw. umbenennen – und prüfen, ob die '
    'Lesestelle woanders hingewandert ist:')

COLUMN_REFERENCE = re.compile(r'\s*(?:\w+\.)?TaxRate\s*')     # 'TaxRate', 'i.TaxRate', 'InvoiceItems.TaxRate' – eine Spaltenangabe für sich
TAX_RATE_WORD = re.compile(r'\bTaxRate\b')
INVOICES_WORD = re.compile(r'\bInvoices\b')                  # die Tabelle; ``InvoiceItems`` (die Positionen) ist ein anderes Wort


class _Readers(ast.NodeVisitor):
    """Sammelt ``(Zeile, umschließende Funktion, Art)`` für jede Lesestelle eines Quelltextes."""

    def __init__(self):
        self.scope = []                  # ('def' | 'class', Name) der umschließenden Funktionen und Klassen, außen zuerst
        self.docstrings = set()          # ``id`` der Zeichenketten, die ein Docstring sind
        self.found = []

    def where(self):
        for kind, name in self.scope:
            if kind == 'def':
                return name
        return f'<class {self.scope[-1][1]}>' if self.scope else '<module>'

    def _note_docstring(self, node):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            self.docstrings.add(id(body[0].value))

    def visit_Module(self, node):
        self._note_docstring(node)
        self.generic_visit(node)

    def _enter(self, kind, node):
        self._note_docstring(node)
        self.scope.append((kind, node.name))
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node):
        self._enter('def', node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self._enter('class', node)

    def _text(self, text, line):
        if COLUMN_REFERENCE.fullmatch(text):
            self.found.append((line, self.where(), 'Spaltenname TaxRate'))
        elif TAX_RATE_WORD.search(text) and INVOICES_WORD.search(text):
            self.found.append((line, self.where(), 'SQL auf Invoices mit TaxRate'))

    def visit_Constant(self, node):
        if isinstance(node.value, str) and id(node) not in self.docstrings:
            self._text(node.value, node.lineno)

    def visit_JoinedStr(self, node):
        self._text(''.join(part.value if isinstance(part, ast.Constant) else '{}' for part in node.values), node.lineno)
        self.generic_visit(node)

    def visit_Subscript(self, node):
        index = node.slice
        if isinstance(index, ast.Constant) and type(index.value) is int and index.value == 35:
            self.found.append((node.lineno, self.where(), 'Spalte 35'))
        elif (isinstance(index, ast.Constant) and index.value == 'tax_rate'
              and isinstance(node.value, ast.Name) and node.value.id == '_INV'):
            self.found.append((node.lineno, self.where(), "Spaltentabelle des E-Rechnungsmodells (_INV['tax_rate'])"))
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if node.attr == 'TAX_RATE':
            self.found.append((node.lineno, self.where(), 'InvoiceCol.TAX_RATE'))
        self.generic_visit(node)

    def visit_Call(self, node):
        if (isinstance(node.func, ast.Name) and node.func.id == 'col' and node.args
                and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'tax_rate'):
            self.found.append((node.lineno, self.where(), "Spaltentabelle des E-Rechnungsmodells (col('tax_rate'))"))
        self.generic_visit(node)


def readers_in(source):
    """Die Lesestellen in einem Quelltext: ``[(Zeile, Funktion, Art)]``."""
    visitor = _Readers()
    visitor.visit(ast.parse(source))
    return visitor.found


def scanned_files():
    for directory in SCANNED_DIRECTORIES:
        yield from sorted((ROOT / directory).rglob('*.py'))
    for name in SCANNED_FILES:
        yield ROOT / name


def find_readers():
    """``{(Datei, Funktion): [(Zeile, Art), …]}`` über alle durchsuchten Dateien (Pfade mit ``/``, relativ zum Projektordner)."""
    found = {}
    for path in scanned_files():
        name = path.relative_to(ROOT).as_posix()
        for line, function, kind in readers_in(path.read_text(encoding='utf-8')):
            found.setdefault((name, function), set()).add((line, kind))
    return {key: sorted(hits) for key, hits in found.items()}


def _entries(keys, found):
    """Die Einträge als Zeilen zum Einfügen, mit dem Fundort als Hinweis."""
    return '\n'.join(f'    {key!r}: \'…\',   # ' + ', '.join(f'{kind} (Zeile {line})' for line, kind in found.get(key, []))
                     for key in keys)


# ── Der Code gegen die Listen ────────────────────────────────────────────────

def test_every_reader_of_the_document_tax_rate_is_on_one_of_the_lists():
    found = find_readers()
    new = sorted(set(found) - set(READERS) - set(NOT_THE_DOCUMENT_RATE))

    assert not new, f'{NEW_READER_MESSAGE}\n{_entries(new, found)}'


def test_no_entry_of_the_lists_is_gone():
    found = find_readers()
    gone = sorted((set(READERS) | set(NOT_THE_DOCUMENT_RATE)) - set(found))

    assert not gone, f'{GONE_READER_MESSAGE}\n' + '\n'.join(f'    {key!r}' for key in gone)


def test_the_two_lists_do_not_overlap_and_every_entry_says_what_it_is():
    assert not set(READERS) & set(NOT_THE_DOCUMENT_RATE)
    for key, text in {**READERS, **NOT_THE_DOCUMENT_RATE}.items():
        assert isinstance(key, tuple) and len(key) == 2 and all(isinstance(part, str) for part in key), key
        assert isinstance(text, str) and len(text.strip()) >= 10, key


def test_the_scan_covers_the_directories_and_files_of_the_plan():
    for directory in SCANNED_DIRECTORIES:
        assert list((ROOT / directory).rglob('*.py')), directory
    for name in SCANNED_FILES:
        assert (ROOT / name).is_file(), name


def test_a_new_reader_breaks_the_test_and_says_what_to_do(monkeypatch):
    monkeypatch.delitem(READERS, ('db/invoices.py', 'insert_invoice'))

    with pytest.raises(AssertionError) as raised:
        test_every_reader_of_the_document_tax_rate_is_on_one_of_the_lists()

    message = str(raised.value)
    assert NEW_READER_MESSAGE in message
    assert "('db/invoices.py', 'insert_invoice')" in message and 'SQL auf Invoices mit TaxRate' in message


def test_a_vanished_reader_breaks_the_test_and_says_what_to_do(monkeypatch):
    monkeypatch.setitem(READERS, ('db/invoices.py', 'no_such_function'), 'erfundene Stelle, die es nicht gibt')

    with pytest.raises(AssertionError) as raised:
        test_no_entry_of_the_lists_is_gone()

    assert GONE_READER_MESSAGE in str(raised.value) and 'no_such_function' in str(raised.value)


# ── Der Scanner selbst: findet, was er finden soll, und nichts sonst ──────────

def _hits(source):
    return sorted({(function, kind) for _line, function, kind in readers_in(source)})


@pytest.mark.parametrize('source, expected', [
    ('def f(invoice):\n    return invoice[35]\n', [('f', 'Spalte 35')]),
    ('def f(quote):\n    return quote[35] if quote[35] is not None else 0.19\n', [('f', 'Spalte 35')]),
    ('def f(rows):\n    return rows[0][35]\n', [('f', 'Spalte 35')]),
    ("def f(row):\n    return row['TaxRate']\n", [('f', 'Spaltenname TaxRate')]),
    ('def f(row):\n    return row.get("TaxRate")\n', [('f', 'Spaltenname TaxRate')]),
    ("def f(self):\n    return self._rate_sql('i.TaxRate')\n", [('f', 'Spaltenname TaxRate')]),
    ("def f():\n    return 'SELECT TaxRate FROM Invoices WHERE ID = ?'\n", [('f', 'SQL auf Invoices mit TaxRate')]),
    ("def f():\n    return ('SELECT InvoiceNumber, TaxRate, '\n            'CustomerId FROM Invoices WHERE ID = ?')\n",
     [('f', 'SQL auf Invoices mit TaxRate')]),
    ("def f(x):\n    return f'UPDATE Invoices SET TaxRate = {x}'\n", [('f', 'SQL auf Invoices mit TaxRate')]),
    ("def f():\n    return '''INSERT INTO Invoices (A,\\n TaxRate) VALUES (?, ?)'''\n", [('f', 'SQL auf Invoices mit TaxRate')]),
    ('def f():\n    return InvoiceCol.TAX_RATE\n', [('f', 'InvoiceCol.TAX_RATE')]),
    ("def build(invoice):\n    def col(name):\n        return invoice[_INV[name]]\n    return col('tax_rate')\n",
     [('build', "Spaltentabelle des E-Rechnungsmodells (col('tax_rate'))")]),
    ("def f(invoice):\n    return invoice[_INV['tax_rate']]\n", [('f', "Spaltentabelle des E-Rechnungsmodells (_INV['tax_rate'])")]),
])
def test_the_scan_finds_each_kind_of_reader(source, expected):
    assert _hits(source) == expected


@pytest.mark.parametrize('source', [
    '"""Das Modul liest Invoices.TaxRate und invoice[35]."""\n',
    'def f():\n    """Liest Invoices.TaxRate und invoice[35]."""\n    return 1\n',
    'class C:\n    """SELECT TaxRate FROM Invoices"""\n',
    'def f(invoice):\n    # invoice[35] ist der Satz; SELECT TaxRate FROM Invoices\n    return invoice[34]\n',
    "def f():\n    return 'SELECT TaxRate FROM Bookings WHERE ID = ?'\n",                   # Buchungssatz
    "def f():\n    return 'SELECT TaxRate FROM InvoiceItems WHERE InvoiceId = ?'\n",         # Position
    "def f():\n    return 'SELECT Code, TaxRate FROM TaxKeys'\n",
    "def f():\n    return 'SELECT SumNet FROM Invoices WHERE ID = ?'\n",                     # Invoices ohne den Satz
    "def f(invoice):\n    return invoice[34], invoice[36], invoice[3], invoice['35']\n",
    "def f(row):\n    return row['TaxRates'], row['MyTaxRate'], row['tax_rate']\n",
])
def test_the_scan_ignores_what_is_no_reader(source):
    assert _hits(source) == []


def test_the_enclosing_function_is_the_outermost_one_and_without_one_the_class_or_the_module():
    source = (
        "FIELDS = ('tax_rate', 'TaxRate')\n"
        "class Page:\n"
        "    MAPPING = {'tax_rate': 'TaxRate'}\n"
        "    def method(self, invoice):\n"
        "        def helper():\n"
        "            return invoice[35]\n"
        "        return helper()\n"
        "def top(invoice):\n"
        "    return [invoice[35] for _ in range(1)]\n")

    assert _hits(source) == [('<class Page>', 'Spaltenname TaxRate'), ('<module>', 'Spaltenname TaxRate'),
                             ('method', 'Spalte 35'), ('top', 'Spalte 35')]
