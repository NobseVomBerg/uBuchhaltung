# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Textregeln für gewöhnliche PDFs: Rechnungsfelder aus dem Text einer Seite lesen. Grundsatz: **lieber leer als geraten.**

Der Text stammt aus einer hochgeladenen Datei. Deshalb arbeiten die Regeln auf Zeilen (``MAX_LINES`` Zeilen zu je höchstens
``MAX_LINE_CHARS`` Zeichen, Leerraum zusammengefasst), alle regulären Ausdrücke laufen in linearer Zeit, und es zählen nur
ASCII-Ziffern (``[0-9]``, nie ``\\d``). Beträge liest allein ``money.parse_amount``.

Jedes gelesene Feld nennt die Zeile, aus der es stammt: ``raw['fields'][<Feld>] = {value, source: 'rule', quote, page}``
(``page`` ist die Seite der Datei, 1-basiert). Die Art (``kind``) ist ``credit_note``, wenn eine der ersten Zeilen so beginnt,
sonst ``invoice`` – ohne Zeile steht dort ``{value, source: 'default'}``.

Bezeichnungen werden ohne Rücksicht auf Groß-/Kleinschreibung gefunden (``re.IGNORECASE`` statt ``casefold``: die Stellen im
Text bleiben dieselben, das Zitat und die Nummer behalten ihre Schreibweise) und stehen als ganze Wörter da (kein Buchstabe
davor oder dahinter).

* **Beträge**: ``1.234,56`` oder ``119,00`` – Komma und genau zwei Nachkommastellen, Tausenderpunkte nur in Dreiergruppen. Nicht
  gelesen wird, was nach einem Prozentsatz aussieht (``19,00 %``), ein Betrag, dessen Vorzeichen nicht eindeutig ist – davor ein
  Minus, Gedankenstrich oder eine öffnende Klammer (``− 119,00``, ``-€ 119,00``, ``(119,00)``), dahinter ein Minus oder ein
  Soll-/Haben-Zeichen (``119,00-``, ``119,00 €-``, ``119,00 CR``), dazwischen nur Leerraum und ein Währungszeichen; ``-119,00``
  unmittelbar davor ist ein negativer Betrag – und eine Zahl, die der Rest einer Gruppierung sein kann (``1 234,56``,
  ``1'234,56``): Ein falscher Betrag wiegt schwerer als ein leeres Feld.
* **Bezeichnung → Betrag**: der erste Betrag HINTER der Bezeichnung in derselben Zeile; steht dazwischen die Gegenbezeichnung
  (Brutto: netto/zzgl, Netto: brutto/inkl), zählt das Paar nicht. Brutto und Netto haben Ränge; nur der beste Rang, der überhaupt
  einen Treffer hat, zählt.
* **Steuer**: Zeilen mit mwst/ust/umsatzsteuer/mehrwertsteuer (nicht die der USt-IdNr. und Steuernummer, und nicht die, in
  denen schon eine Brutto- oder Nettobezeichnung den Betrag beansprucht: „Gesamtbetrag inkl. 19 % MwSt. 119,00“). Der Betrag steht
  HINTER dem Steuerwort: Steht dort genau einer, ist er die Steuer – außer ein „auf“, „von“ oder „aus“ steht zwischen dem
  Steuerwort und ihm (``19 % MwSt auf 100,00``: er ist die Basis). Stehen dort mehrere (``USt 19% 100,00 19,00 119,00``) und nennt
  die Zeile genau einen Satz, ist die Steuer der Betrag, der als einziger die Satzprobe gegen einen ANDEREN Betrag der Zeile
  besteht (Basis × Satz = Steuer auf den Cent, oder die im Brutto enthaltene Steuer); ohne Satz, ohne einen oder mit mehreren
  solchen Beträgen liefert die Zeile keine Steuer. Mehrere Sätze ergeben ``raw['tax_breakdown']``.
* **Währung**: Maßgeblich ist, was unmittelbar am gelesenen Betrag steht (davor oder dahinter, nur Leerraum dazwischen): ``€`` oder
  ``EUR`` ist Euro, ``$``, ``£``, ``USD``, ``CHF`` und ``GBP`` machen den Betrag fremd. Das gilt für das Brutto, ohne Brutto für
  Netto und Steuer. Steht am Betrag nichts, gilt der ganze Text: Euro, wenn er Euro nennt – aber nennt er ein fremdes Kürzel an
  einer Zahl und Euro nur außerhalb von Zahlen (``EUR-Konto``), sind die Beträge fremd.
* **Mehrere Werte für ein Feld**: Sind alle gleich, gilt der Wert. Sind sie verschieden, bleibt das Feld leer (``conflict_<Feld>``)
  – bei Brutto, Netto und Steuer nur dann nicht, wenn GENAU EINE Kombination die Summenprobe besteht (``by_checksum``).
* **Summenprobe** (``raw['checks']``): ``sum`` = Netto + Steuer = Brutto, ``rate`` = Netto × Satz = Steuer auf den Cent (nur bei
  genau einem Satz), je ``ok``, ``mismatch`` oder ``incomplete``.
"""
import re
from bisect import bisect_left
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from itertools import product, takewhile
from typing import Optional

import receipt_reading as package
from money import SCALE, parse_amount, tax_from_gross, tax_from_net, to_minor

from . import NAME_LIMIT, NUMBER_LIMIT, RAW_LIST_LIMIT, RAW_STRING_LIMIT, VAT_ID_LIMIT, Reading, clean_text

MAX_LINES = 5000                 # so viele Zeilen werden höchstens gelesen
MAX_LINE_CHARS = 300             # so viele Zeichen je Zeile
SENDER_LINES = 15                # Absender: die ersten Zeilen der ersten, die letzten der letzten gelesenen Seite
KIND_LINES = 20                  # Art: so viele Zeilen vom Anfang
MAX_SENDER_CHARS = 80            # ein Name ist nicht länger
MAX_CANDIDATES = 8               # so viele verschiedene Werte je Feld werden noch gegeneinander geprüft
MAX_RATES = 8                    # so viele Steuersätze eines Belegs werden noch gelesen
MAX_LINE_AMOUNTS = 8             # so viele Beträge darf eine Steuerzeile nennen, damit die Satzprobe zwischen ihnen entscheidet
YEARS = (1990, 2100)             # ein Belegdatum liegt in diesen Jahren
CENT = 10 ** (SCALE - 2)         # ein Cent in Minor Units
_LINE_HEAD = MAX_LINE_CHARS * 4  # so viel einer Zeile wird angesehen, bevor der Leerraum zusammengefasst wird

_SPACES = str.maketrans({'\N{NO-BREAK SPACE}': ' ', '\N{NARROW NO-BREAK SPACE}': ' '})


# ── Zeilen ───────────────────────────────────────────────────────────────────

def _words(*alternatives: str) -> re.Pattern:
    """Eine der Bezeichnungen als ganzes Wort, ohne Rücksicht auf Groß-/Kleinschreibung. Die Wortgrenze ist selbst gebaut: ``\\b``
    zählt Ziffern und ``_`` zu den Wörtern, hier zählen nur Buchstaben (``19%MwSt`` und ``MwSt19`` sind Treffer)."""
    return re.compile(r'(?<![^\W\d_])(?:' + '|'.join(alternatives) + r')(?![^\W\d_])', re.IGNORECASE)


# Der Apostroph vor der Zahl (``1'234,56``) ist wie ein Punkt, Komma oder eine Ziffer: Dann ist es der Rest einer Zahl
_AMOUNT = re.compile(r"(?<![0-9.,'\N{RIGHT SINGLE QUOTATION MARK}])-?(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+),[0-9]{2}(?![0-9])")
_PERCENT = re.compile(r' ?%')
_DASH = '-\N{MINUS SIGN}\N{EN DASH}\N{EM DASH}'
# Das Vorzeichen eines Betrags ist nur eindeutig, wenn es unmittelbar vor der Zahl steht (``-119,00``). Davor ein Minus, Gedankenstrich
# oder eine öffnende Klammer, dahinter ein Minus oder ein Soll-/Haben-Zeichen (``CR``, ``DR``, ``S``, ``H``) – dazwischen nur Leerraum
# und ein Währungszeichen (``- € 119,00``, ``(€ 119,00)``, ``119,00 €-``) – lässt es offen: Der Betrag wird nicht gelesen. Ein Minus,
# dem ein Buchstabe folgt (``119,00 EUR-Konto``), ist ein Bindestrich, und eins mit Leerraum davor und einer Ziffer dahinter
# (``119,00 -5,00``) gehört zur nächsten Zahl. Alle Muster haben nur beschränkte Teile: lineare Laufzeit.
_MARK = r'(?:€|EUR|\$|USD|CHF|GBP|£)'
_SIGN_BEFORE = re.compile(r'[' + _DASH + r'(] ?(?:' + _MARK + r' ?)?\Z')
_SIGN_AFTER = re.compile(r' ?(?:' + _MARK + r' ?)?(?:(?<! )[' + _DASH + r'](?![A-Za-z])|(?<= )[' + _DASH + r'](?![A-Za-z0-9])'
                         r'|(?:CR|DR|S|H)(?![A-Za-z]))')
_SIGN_REACH = 6                  # so weit vor dem Betrag wird nach einem Vorzeichen gesucht: Minus, Leerraum, ``EUR``, Leerraum
_GROUP_BEFORE = re.compile(r'(?<![0-9.,])[0-9]{1,3} \Z')
# Was unmittelbar an einem Betrag steht (davor oder dahinter, nur ein Leerzeichen dazwischen): ein Euro- oder ein fremdes Zeichen
_MARK_AFTER = re.compile(r' ?(?:(?P<euro>€|EUR(?![A-Za-z]))|(?P<foreign>[$£]|(?:USD|CHF|GBP)(?![A-Za-z])))')
_MARK_BEFORE = re.compile(r'(?:(?P<euro>€|(?<![A-Za-z])EUR)|(?P<foreign>[$£]|(?<![A-Za-z])(?:USD|CHF|GBP))) ?\Z')
_MARK_REACH = 4                  # so weit vor dem Betrag wird nach einem Währungszeichen gesucht: ``EUR`` und ein Leerzeichen


def _scan_amounts(text: str) -> list:
    """``[(Stelle, Ende, Minor Units)]`` der Beträge einer Zeile (siehe Kopf des Moduls), in der Reihenfolge der Zeile."""
    found = []
    for match in _AMOUNT.finditer(text):
        start, end = match.span()
        digits = match.group()
        if _PERCENT.match(text, end) or _SIGN_AFTER.match(text, end):            # ``19,00 %`` ist ein Satz, ``119,00-`` hat kein klares Vorzeichen
            continue
        if not digits.startswith('-') and _SIGN_BEFORE.search(text, max(0, start - _SIGN_REACH), start):
            continue
        integer = digits.lstrip('-').split(',')[0]
        if len(integer) == 3 and _GROUP_BEFORE.search(text, max(0, start - 5), start):
            continue
        try:
            found.append((start, end, to_minor(parse_amount(digits))))
        except ValueError:                                  # zu groß
            continue
    return found


def _currency_at(text: str, start: int, end: int) -> Optional[str]:
    """Was unmittelbar am Betrag ``text[start:end]`` steht: ``'EUR'`` (Euro-Zeichen oder -Kürzel), ``'foreign'`` (``$``, ``£``,
    ``USD``, ``CHF``, ``GBP``) oder ``None``. Steht beides da, gilt ``'foreign'``."""
    found = [mark for mark in (_MARK_BEFORE.search(text, max(0, start - _MARK_REACH), start), _MARK_AFTER.match(text, end)) if mark]
    if any(mark.group('foreign') for mark in found):
        return 'foreign'
    return 'EUR' if found else None


class _Line:
    """Eine Zeile (Leerraum zusammengefasst) und die Seite der Datei, auf der sie steht."""
    __slots__ = ('text', 'page', '_found', '_starts')

    def __init__(self, text: str, page: int):
        self.text = text
        self.page = page
        self._found = None
        self._starts = None

    def amounts(self) -> list:
        """``[(Stelle, Ende, Minor Units)]`` der Beträge der Zeile, einmal gelesen."""
        if self._found is None:
            self._found = _scan_amounts(self.text)
            self._starts = [start for start, _end, _minor in self._found]
        return self._found

    def amount_after(self, position: int):
        """``(Stelle, Ende, Betrag)`` des ersten Betrags ab ``position``, sonst ``None``."""
        found = self.amounts()
        index = bisect_left(self._starts, position)
        return found[index] if index < len(found) else None


def _lines(pages) -> list:
    """Die Zeilen aller Seiten in der Reihenfolge der Datei, ohne leere. ``pages.numbers`` (``pdf_text.PageTexts``) nennt die
    Seiten der Datei; ohne sie zählen die Seiten von 1 an."""
    numbers = getattr(pages, 'numbers', None) or []
    lines = []
    for position, page in enumerate(pages):
        number = numbers[position] if position < len(numbers) else position + 1
        for raw in str(page).splitlines():
            text = ' '.join(raw[:_LINE_HEAD].translate(_SPACES).split())[:MAX_LINE_CHARS]
            if text:
                lines.append(_Line(text, number))
                if len(lines) >= MAX_LINES:
                    return lines
    return lines


def _quote(text: str) -> str:
    return clean_text(text, RAW_STRING_LIMIT) or ''


@dataclass
class _Hit:
    value: object
    text: str
    page: int
    currency: Optional[str] = None  # bei einem Betrag: was unmittelbar an ihm steht (``_currency_at``)


def _distinct(hits: list) -> list:
    """Die Treffer mit je einem Wert, der erste jedes Werts, in der Reihenfolge des Dokuments."""
    first = {}
    for hit in hits:
        first.setdefault(hit.value, hit)
    return list(first.values())


def _best_rank(lines: list, ranks: list, pick) -> list:
    """Die Treffer des besten Rangs, der überhaupt einen hat. ``ranks``: je Rang ein Muster für die Bezeichnung;
    ``pick(Zeile, Treffer der Bezeichnung)`` liefert den Wert dahinter, einen fertigen ``_Hit`` oder ``None``."""
    for pattern in ranks:
        hits = []
        for line in lines:
            for label in pattern.finditer(line.text):
                value = pick(line, label)
                if value is not None:
                    hits.append(value if isinstance(value, _Hit) else _Hit(value, line.text, line.page))
        if hits:
            return hits
    return []


# ── Beträge: Brutto, Netto ───────────────────────────────────────────────────

_GROSS = [
    _words('rechnungsbetrag', 'gesamtbetrag', 'bruttobetrag', 'endbetrag', 'gesamtsumme', 'summe brutto', 'gesamt brutto'),
    _words('zahlbetrag', 'zu zahlender betrag', 'zu zahlen'),
    _words('brutto', 'total'),
]
_NET = [
    _words('summe netto', 'nettobetrag', 'gesamt netto', 'nettosumme'),
    _words('zwischensumme'),
    _words('netto'),
]
_NOT_GROSS = re.compile(r'netto|zzgl', re.IGNORECASE)
_NOT_NET = re.compile(r'brutto|inkl', re.IGNORECASE)
# Eine Zeile, in der schon eine Brutto- oder Nettobezeichnung den Betrag beansprucht, gibt keinen Steuerbetrag her
_OWNS_AMOUNT = [_GROSS[0], _GROSS[1], _words('brutto'), *_NET]


def _amount_after(counter: re.Pattern):
    def pick(line: _Line, label):
        found = line.amount_after(label.end())
        if found is None or counter.search(line.text, label.end(), found[0]):
            return None
        return _Hit(found[2], line.text, line.page, _currency_at(line.text, found[0], found[1]))
    return pick


# ── Steuer ───────────────────────────────────────────────────────────────────

_TAX_WORD = _words('mwst', 'ust', 'umsatzsteuer', 'mehrwertsteuer')
_TAX_IDENTIFIER = re.compile(r'ust-id|ustid|ust\.-id|umsatzsteuer-id|steuernummer|steuer-nr', re.IGNORECASE)
_RATE = re.compile(r'(?<![0-9.,])([0-9]{1,2}(?:,[0-9]{1,2})?) ?%')
_BASIS_WORD = _words('auf', 'von', 'aus')    # ``19 % MwSt auf 100,00``: Der Betrag dahinter ist die Basis, nicht die Steuer


@dataclass
class _TaxLine:
    rate: Optional[Decimal]        # in Prozent (19), ``None``: die Zeile nennt keinen Satz
    value: int
    text: str
    page: int
    currency: Optional[str] = None  # was unmittelbar am Betrag steht (``_currency_at``)


@dataclass
class _TaxChoice:
    """Eine Möglichkeit, die Steuer des Belegs zu lesen: die Zeilen, aus denen sie sich zusammensetzt, und ihre Summe."""
    value: int
    lines: list


def _matches_net(net: int, tax: int, rate) -> bool:
    """Netto × Satz = Steuer, auf den Cent – die Satzprobe. ``rate`` in Prozent."""
    return abs(tax_from_net(net, rate) - tax) <= CENT


def _tax_fits(tax: int, basis: int, rate) -> bool:
    """Passt ``tax`` zu ``basis`` und dem Satz, auf den Cent: als Steuer auf ein Netto (die Satzprobe) oder als die im Brutto
    enthaltene Steuer (Brutto × Satz / (1 + Satz))?"""
    return _matches_net(basis, tax, rate) or abs(tax_from_gross(basis, rate) - tax) <= CENT


def _tax_amount(line: _Line, word, rate: Optional[Decimal]):
    """Der Steuerbetrag einer Zeile mit Steuerwort (``word``: der Treffer) als ``(Stelle, Ende, Minor Units)`` – oder ``None``: Die Zeile
    liefert keine Steuer. Steht HINTER dem Steuerwort genau ein Betrag, ist er es – außer ein „auf“, „von“ oder „aus“ steht dazwischen,
    dann ist er die Basis. Stehen dort mehrere, entscheidet die Satzprobe: Gilt in der Zeile genau ein Satz, ist es der Betrag, der als
    einziger (gleiche Werte zählen einmal) zu einem ANDEREN Betrag der Zeile passt; sonst – ohne Satz, mit mehreren Sätzen oder
    mehr als ``MAX_LINE_AMOUNTS`` Beträgen (eine Tabellenzeile; die Probe wäre quadratisch) – liefert die Zeile keine Steuer."""
    amounts = line.amounts()
    after = [amount for amount in amounts if amount[0] >= word.start()]
    if not after:
        return None
    if len(after) == 1:
        return None if _BASIS_WORD.search(line.text, word.end(), after[0][0]) else after[0]
    if rate is None or len(amounts) > MAX_LINE_AMOUNTS:
        return None
    fitting = [amount for amount in after
               if any(_tax_fits(amount[2], other[2], rate) for other in amounts if other[0] != amount[0])]
    return fitting[0] if len({amount[2] for amount in fitting}) == 1 else None


def _tax_lines(lines: list) -> list:
    found = []
    for line in lines:
        word = _TAX_WORD.search(line.text)
        if word is None or _TAX_IDENTIFIER.search(line.text) or any(p.search(line.text) for p in _OWNS_AMOUNT):
            continue
        rates = {Decimal(match.group(1).replace(',', '.')) for match in _RATE.finditer(line.text)}
        rate = rates.pop() if len(rates) == 1 else None
        amount = _tax_amount(line, word, rate)
        if amount is not None:
            found.append(_TaxLine(rate, amount[2], line.text, line.page, _currency_at(line.text, amount[0], amount[1])))
    return found


def _tax_choices(tax_lines: list) -> Optional[list]:
    """Die Möglichkeiten, die Steuer zu lesen: die Zeilen mit Satz zusammen (je Satz ein Betrag), jeder Betrag einer Zeile ohne
    Satz für sich; gleiche Summen nur einmal. Gibt es je Satz mehrere Beträge, gibt es mehrere Kombinationen. ``None``: zu viele."""
    rated, unrated = {}, {}
    for line in tax_lines:
        if line.rate is None:
            unrated.setdefault(line.value, line)
        else:
            rated.setdefault(line.rate, {}).setdefault(line.value, line)
    choices = []
    if rated:
        groups = [list(by_value.values()) for by_value in rated.values()]
        combinations = 1
        for group in groups:
            combinations *= len(group)
        if len(groups) > MAX_RATES or combinations > MAX_CANDIDATES:
            return None
        choices += [_TaxChoice(sum(line.value for line in combo), list(combo)) for combo in product(*groups)]
    choices += [_TaxChoice(value, [line]) for value, line in unrated.items()]
    first = {}
    for choice in choices:
        first.setdefault(choice.value, choice)
    return list(first.values())


def _resolve_money(gross: list, net: list, tax: list):
    """Brutto, Netto und Steuer aus den Möglichkeiten (je Feld Treffer mit verschiedenen Werten). Gibt ``(gewählt, offen,
    by_checksum)`` zurück: ``gewählt[Feld]`` ist die Möglichkeit, ``offen`` die Felder, die leer bleiben. Mehrere
    Möglichkeiten für ein Feld entscheidet die Summenprobe – wenn GENAU EINE Kombination sie besteht."""
    pools = {'gross_minor': gross, 'net_minor': net, 'tax_minor': tax}
    chosen = {name: items[0] for name, items in pools.items() if len(items) == 1}
    undecided = [name for name, items in pools.items() if len(items) > 1]
    if not undecided:
        return chosen, [], False
    if all(items and len(items) <= MAX_CANDIDATES for items in pools.values()):
        passing = [combo for combo in product(gross, net, tax) if combo[1].value + combo[2].value == combo[0].value]
        if len(passing) == 1:
            return dict(zip(pools, passing[0])), [], True
    return chosen, undecided, False


# ── Datum ────────────────────────────────────────────────────────────────────

_DATE_LABELS = [_words('rechnungsdatum'), _words('belegdatum', 'ausstellungsdatum', 'datum der rechnung'), _words('datum')]
_DATE_EXCLUDED = re.compile(r'leistungs|liefer|fällig|faellig|bestell|auftrag|zahlbar|valuta', re.IGNORECASE)
_DATE_DE = re.compile(r'(?<![0-9.])([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})(?![0-9])')
_DATE_ISO = re.compile(r'(?<![0-9-])([0-9]{4})-([0-9]{2})-([0-9]{2})(?![0-9])')
_DATE_SHAPE = re.compile(r'[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2,4}|[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _date_after(line: _Line, label) -> Optional[str]:
    """Das erste Datum hinter der Bezeichnung als ISO-Text; ist es keins (31.02.2026, ein Jahr außerhalb), ``None``."""
    end = label.end()
    german, iso = _DATE_DE.search(line.text, end), _DATE_ISO.search(line.text, end)
    if german is None and iso is None:
        return None
    if iso is None or (german is not None and german.start() < iso.start()):
        day, month, year = (int(part) for part in german.groups())
    else:
        year, month, day = (int(part) for part in iso.groups())
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    return found.isoformat() if YEARS[0] <= found.year <= YEARS[1] else None


# ── Rechnungsnummer ──────────────────────────────────────────────────────────

_NUMBER_LABEL = _words('rechnungsnummer', 'rechnungs-nr', 'rechnung nr', 'rechnungsnr', 'rechnung-nr', 'invoice no')
_NUMBER_SKIP = re.compile(r'[.: ]{0,4}')
_NUMBER_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9/_.-]{0,39}(?![A-Za-z0-9/_.-])')


def _number_after(line: _Line, label) -> Optional[str]:
    """Das Token hinter der Bezeichnung, wenn es eine Ziffer hat und kein Datum ist."""
    start = _NUMBER_SKIP.match(line.text, label.end()).end()
    token = _NUMBER_TOKEN.match(line.text, start)
    value = token.group().rstrip('.') if token else ''
    if not value or not any(char in '0123456789' for char in value) or _DATE_SHAPE.fullmatch(value):
        return None
    return value


# ── USt-IdNr. ────────────────────────────────────────────────────────────────

# Die Tabelle von ``export.einvoice_model`` (``tax_id_kind``): Wer dieses Modul importiert, zieht das ganze ``db``-Paket nach
# (sqlite3, Datenbank-Mixins) – für ein reines Lesepaket und einen Kindprozess zu schwer. ``tests/test_receipt_reading_text.py``
# hält beide gleich.
_VAT_ID_RULES = {
    'AT': r'U\d{8}', 'BE': r'\d{9,10}', 'BG': r'\d{9,10}', 'CY': r'\d{8}[A-Z]', 'CZ': r'\d{8,10}', 'DE': r'\d{9}',
    'DK': r'\d{8}', 'EE': r'\d{9}', 'EL': r'\d{9}', 'ES': r'[A-Z0-9]\d{7}[A-Z0-9]', 'FI': r'\d{8}',
    'FR': r'[A-HJ-NP-Z0-9]{2}\d{9}', 'HR': r'\d{11}', 'HU': r'\d{8}', 'IE': r'\d{7}[A-Z]{1,2}|\d[A-Z0-9+*]\d{5}[A-Z]',
    'IT': r'\d{11}', 'LT': r'\d{9}|\d{12}', 'LU': r'\d{8}', 'LV': r'\d{11}', 'MT': r'\d{8}', 'NL': r'\d{9}B\d{2}',
    'PL': r'\d{10}', 'PT': r'\d{9}', 'RO': r'\d{2,10}', 'SE': r'\d{12}', 'SI': r'\d{8}', 'SK': r'\d{10}',
    'XI': r'\d{9}|\d{12}|GD\d{3}|HA\d{3}', 'GB': r'\d{9}|\d{12}|GD\d{3}|HA\d{3}',
    'CHE': r'\d{9}(?:MWST|TVA|IVA)?', 'NO': r'\d{9}(?:MVA)?',
}
_VAT_ID_PATTERNS = {prefix: re.compile(rest, re.ASCII) for prefix, rest in _VAT_ID_RULES.items()}
_VAT_SEPARATORS = re.compile(r'[\s.\-]')
_VAT_LABEL = _words(r'ust[ .\-]*id(?:[ .\-]*nr)?', r'uid(?:[ .\-]*nr)?', r'vat(?:[ .\-]*(?:id|no|number|nr))?',
                    r'umsatzsteuer[ .\-]*(?:identifikationsnummer|idnr|id)')
_VAT_DE = re.compile(r'(?<![A-Za-z0-9])DE ?[0-9]{3} ?[0-9]{3} ?[0-9]{3}(?![A-Za-z0-9])')
_VAT_PIECE = re.compile(r'[A-Za-z0-9.\-]+')
_VAT_PIECES = 5                  # so viele Stücke hinter der Bezeichnung werden zu einer Nummer zusammengesetzt
_VAT_REST = 80                   # so viel der Zeile hinter der Bezeichnung wird angesehen


def vat_id_value(text) -> Optional[str]:
    """Die USt-IdNr., die ``text`` ist – ohne Leerzeichen, Punkte und Bindestriche, in Großschreibung –, sonst ``None``: das
    Länderkürzel eines Landes und der Rest nach dem Muster dieses Landes (wie ``export.einvoice_model.tax_id_kind``)."""
    compact = _VAT_SEPARATORS.sub('', str(text)).upper()
    prefix = 'CHE' if compact.startswith('CHE') else compact[:2]
    rule = _VAT_ID_PATTERNS.get(prefix)
    return compact if rule is not None and rule.fullmatch(compact[len(prefix):]) else None


def _vat_after(line: _Line, label) -> Optional[str]:
    """Die USt-IdNr. hinter der Bezeichnung: die ersten Stücke bis zur ersten gültigen Nummer („DE 123 456 789“ sind vier
    Stücke), aber nie über ein Stück mit einem anderen Zeichen hinweg („IBAN:“)."""
    rest = line.text[label.end():label.end() + _VAT_REST].lstrip(' .:-')
    pieces = []
    for piece in rest.split()[:_VAT_PIECES]:
        piece = piece.strip(',;()[]')
        if not _VAT_PIECE.fullmatch(piece):
            break
        pieces.append(piece)
        value = vat_id_value(''.join(pieces))
        if value is not None:
            return value
    return None


def _vat_hits(lines: list, own_ids: set) -> list:
    hits = []
    for line in lines:
        values = [_vat_after(line, label) for label in _VAT_LABEL.finditer(line.text)]
        values += [match.group().replace(' ', '') for match in _VAT_DE.finditer(line.text)]
        hits += [_Hit(value, line.text, line.page) for value in values if value is not None and value not in own_ids]
    return hits


# ── Absender ─────────────────────────────────────────────────────────────────

_FORMS = _words('gmbh', 'ag', 'ug', 'kg', 'ohg', 'gbr', r'e\.k\.', 'mbh', 'se', 'eg', r'e\.v\.', 'ltd', 'inc')
_SECTION_SPLIT = re.compile(r' · | \| | – |, ')
_FIVE_DIGITS = re.compile(r'[0-9]{5}')


def _normalized(text: str) -> str:
    return ' '.join(text.split()).casefold()


def _sender_candidates(lines: list, own_names: list) -> list:
    """Die Abschnitte der ersten Zeilen der ersten Seite, die wie ein Firmenname aussehen (Rechtsform als ganzes Wort, kurz,
    ohne Postleitzahl, ohne ``:`` und ``@`` – ein Name ist keine Kontaktzeile –, nicht die eigene Firma)."""
    first_page = lines[0].page
    found = {}
    for line in list(takewhile(lambda item: item.page == first_page, lines))[:SENDER_LINES]:
        for section in _SECTION_SPLIT.split(line.text):
            section = clean_text(section, NAME_LIMIT) or ''
            key = _normalized(section)
            if (section and len(section) <= MAX_SENDER_CHARS and _FORMS.search(section) and not _FIVE_DIGITS.search(section)
                    and ':' not in section and '@' not in section
                    and not any(key == own or own in key for own in own_names)):
                found.setdefault(key, _Hit(section, line.text, line.page))
    return list(found.items())


def _sender(lines: list, own_names: list, notes: list) -> Optional[_Hit]:
    candidates = _sender_candidates(lines, own_names)
    if len(candidates) == 1:
        return candidates[0][1]
    if not candidates:
        return None
    last_page = lines[-1].page
    foot = [_normalized(line.text) for line in lines if line.page == last_page][-SENDER_LINES:]
    in_foot = [hit for key, hit in candidates if any(key in line for line in foot)]
    if len(in_foot) == 1:
        return in_foot[0]
    notes.append('conflict_sender_name')
    return None


# ── Währung, Art ─────────────────────────────────────────────────────────────

_EURO = re.compile(r'€|(?<![A-Za-z])EUR(?![A-Za-z])')
# Ein Euro- oder ein fremdes Währungszeichen unmittelbar an einer Zahl (davor oder dahinter, höchstens ein Leerzeichen dazwischen)
_EURO_AT_NUMBER = re.compile(r'(?:€|(?<![A-Za-z])EUR) ?[0-9]|[0-9] ?(?:€|EUR(?![A-Za-z]))')
_FOREIGN = re.compile(r'(?:(?<![A-Za-z])(?:USD|CHF|GBP)|[$£]) ?[0-9]|[0-9] ?(?:[$£]|(?:USD|CHF|GBP)(?![A-Za-z]))')
_CREDIT_NOTE = re.compile(r'gutschrift|stornorechnung|rechnungskorrektur', re.IGNORECASE)


def _foreign_text(lines: list) -> bool:
    """Nennt der Text ein fremdes Währungskürzel an einer Zahl und Euro nirgends an einer Zahl? Ein ``EUR`` außerhalb von Zahlen
    (``EUR-Konto``, ``Preise in EUR``) macht Beträge in ``USD`` nicht zu Euro."""
    return any(_FOREIGN.search(line.text) for line in lines) and not any(_EURO_AT_NUMBER.search(line.text) for line in lines)


# ── Einstieg ─────────────────────────────────────────────────────────────────

def _own(own) -> tuple:
    """``(normalisierte Namen, USt-IdNrn.)`` der eigenen Firmen; leere Namen zählen nicht (der leere Name steckt in jedem)."""
    try:
        names, vat_ids = own
        names = [_normalized(name) for name in names if isinstance(name, str)]
        ids = {_VAT_SEPARATORS.sub('', value).upper() for value in vat_ids if isinstance(value, str)}
    except (TypeError, ValueError):
        names, ids = [], set()
    return [name for name in names if name], ids


def _single(hits: list, name: str, notes: list) -> Optional[_Hit]:
    """Der Treffer, wenn alle Treffer denselben Wert haben; mehrere verschiedene sind leer und ein Befund."""
    distinct = _distinct(hits)
    if len(distinct) == 1:
        return distinct[0]
    if distinct:
        notes.append(f'conflict_{name}')
    return None


def _field(value, origin) -> dict:
    """Ein gelesenes Feld in ``raw['fields']``; ``origin``: alles mit ``text`` und ``page`` (Zeile, Treffer, Steuerzeile)."""
    return {'value': value, 'source': 'rule', 'quote': _quote(origin.text), 'page': origin.page}


def _check_sum(gross, net, tax) -> str:
    if None in (gross, net, tax):
        return 'incomplete'
    return 'ok' if net + tax == gross else 'mismatch'


def _check_rate(net, tax, choice: Optional[_TaxChoice]) -> str:
    """Netto × Satz gegen die Steuer, auf den Cent – nur, wenn der Beleg genau einen Satz nennt (sonst fehlt die Basis)."""
    if net is None or tax is None or choice is None or len(choice.lines) != 1 or choice.lines[0].rate is None:
        return 'incomplete'
    return 'ok' if _matches_net(net, tax, choice.lines[0].rate) else 'mismatch'


def read_text(pages, own) -> Reading:
    """Die Rechnungsfelder aus den Seitentexten (``pdf_text.extract_pdf_text``) lesen. ``own``: ``(Namen der eigenen Firmen,
    deren USt-IdNrn. ohne Leerzeichen)`` – was dem Beleg gehört, wird nicht als Absender gelesen. Das Ergebnis hat die Quelle
    ``text``, den Status ``ok`` (mindestens ein Feld gelesen) oder ``empty``, die Zitate und Proben in ``raw`` und die Befunde in
    ``notes``; ``text`` sind die Seiten hintereinander, gekürzt."""
    lines = _lines(pages)
    own_names, own_ids = _own(own)
    notes: list = []
    fields: dict = {}
    text = clean_text('\n'.join(str(page) for page in pages), package.MAX_TEXT_CHARS)
    if not lines:
        return Reading(status='empty', source='text', text=text)

    date_hit = _single(_best_rank([line for line in lines if not _DATE_EXCLUDED.search(line.text)], _DATE_LABELS, _date_after),
                       'doc_date', notes)
    number_hit = _single(_best_rank(lines, [_NUMBER_LABEL], _number_after), 'invoice_number', notes)
    vat_hit = _single(_vat_hits(lines, own_ids), 'sender_vat_id', notes)
    sender_hit = _sender(lines, own_names, notes)

    gross_hits = _distinct(_best_rank(lines, _GROSS, _amount_after(_NOT_GROSS)))
    net_hits = _distinct(_best_rank(lines, _NET, _amount_after(_NOT_NET)))
    tax_choices = _tax_choices(_tax_lines(lines))
    if tax_choices is None:
        notes.append('conflict_tax_minor')
        tax_choices = []
    chosen, undecided, by_checksum = _resolve_money(gross_hits, net_hits, tax_choices)
    notes += [f'conflict_{name}' for name in undecided]
    if by_checksum:
        notes.append('by_checksum')
    gross_hit, net_hit, tax_choice = (chosen.get(name) for name in ('gross_minor', 'net_minor', 'tax_minor'))

    # Die Währung entscheidet sich an den Beträgen, die übernommen werden: am Brutto, ohne Brutto an Netto und Steuer. Steht dort ein
    # fremdes Zeichen, sind die Beträge fremd; steht ein Euro-Zeichen da, sind sie Euro. Steht nichts dran, gilt der ganze Text.
    tax_lines = tax_choice.lines if tax_choice is not None else []
    decisive = [gross_hit] if gross_hit is not None else [hit for hit in (net_hit, *tax_lines) if hit is not None]
    marks = {hit.currency for hit in decisive}
    euro = next((line for line in lines if _EURO.search(line.text)), None)
    if 'foreign' in marks or ('EUR' not in marks and _foreign_text(lines)):
        notes.append('foreign_currency')
        gross_hit = net_hit = tax_choice = None
        marks = set()
    gross, net = (hit.value if hit else None for hit in (gross_hit, net_hit))
    tax = tax_choice.value if tax_choice else None

    values = {
        'doc_date': date_hit, 'invoice_number': number_hit, 'sender_vat_id': vat_hit, 'sender_name': sender_hit,
        'gross_minor': gross_hit, 'net_minor': net_hit,
    }
    for name, hit in values.items():
        if hit is not None:
            fields[name] = _field(hit.value, hit)
    breakdown = None
    if tax_choice is not None:
        first = tax_choice.lines[0]
        fields['tax_minor'] = _field(tax, first)
        if len(tax_choice.lines) > 1:
            breakdown = [{'rate': None if line.rate is None else float(line.rate / 100), 'category': None, 'basis_minor': None,
                          'tax_minor': line.value, 'quote': _quote(line.text), 'page': line.page}
                         for line in tax_choice.lines[:RAW_LIST_LIMIT]]
        elif first.rate is not None:
            fields['tax_rate'] = _field(float(first.rate / 100), first)
    if any(value is not None for value in (gross, net, tax)):
        if 'EUR' in marks:                                  # das Zeichen steht am Betrag: Zitat ist dessen Zeile
            fields['currency'] = _field('EUR', next(hit for hit in decisive if hit.currency == 'EUR'))
        elif euro is not None:
            # Zitat: die Zeile eines gelesenen Betrags, wenn sie das Euro-Zeichen trägt, sonst die erste Zeile mit einem
            amount_origins = [hit for hit in (gross_hit, net_hit, tax_choice.lines[0] if tax_choice else None) if hit is not None]
            fields['currency'] = _field('EUR', next((hit for hit in amount_origins if _EURO.search(hit.text)), euro))

    if not fields:
        return Reading(status='empty', source='text', text=text, notes=list(dict.fromkeys(notes)))
    credit = next((line for line in lines[:KIND_LINES] if _CREDIT_NOTE.match(line.text)), None)
    kind = 'credit_note' if credit else 'invoice'
    fields['kind'] = _field(kind, credit) if credit else {'value': kind, 'source': 'default'}
    raw = {'fields': fields, 'checks': {'sum': _check_sum(gross, net, tax), 'rate': _check_rate(net, tax, tax_choice)}}
    if breakdown:
        raw['tax_breakdown'] = breakdown
    return Reading(
        status='ok', source='text', doc_date=date_hit.value if date_hit else None, gross_minor=gross, net_minor=net,
        tax_minor=tax, tax_rate=fields['tax_rate']['value'] if 'tax_rate' in fields else None,
        currency='EUR' if 'currency' in fields else None,
        sender_name=clean_text(sender_hit.value, NAME_LIMIT) if sender_hit else None,
        sender_vat_id=clean_text(vat_hit.value, VAT_ID_LIMIT) if vat_hit else None,
        invoice_number=clean_text(number_hit.value, NUMBER_LIMIT) if number_hit else None, kind=kind, raw=raw, text=text,
        notes=list(dict.fromkeys(notes)))
