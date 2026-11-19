# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Kontoauszug-Import: Umsatzliste der Deutschen Kreditbank (DKB) als CSV.

Die DKB hat ihren Export gewechselt und kann es wieder tun. Deshalb wird nichts am genauen Namen einer Spalte erkannt
(``MEANS``), sondern an ihrer *Bedeutung*; gebraucht werden vier – der Tag, die Gegenseite, der Verwendungszweck und der
Betrag –, dazu die IBAN der Gegenseite und der Status, wo die Datei sie hat. Alles andere darf kommen, gehen oder
umbenannt werden. Welches Format eine Datei hat, steht in ihr selbst; es wird nie von Hand gewählt.

Zwei bekannte Formate – jeder ihrer Unterschiede machte allein eine Datei unlesbar:

* **neu:** Vorspann ``"Girokonto";"DE…"``, ``"Zeitraum:"``, ``"Kontostand vom …"``; Kopf ``Buchungsdatum;Wertstellung;Status;
  Zahlungspflichtige*r;Zahlungsempfänger*in;Verwendungszweck;Umsatztyp;IBAN;Betrag (€);…``, Datum ``TT.MM.JJ``, UTF-8 mit
  BOM. Die Gegenseite steht in zwei Spalten: bei einer Belastung (Betrag < 0) ist es der Zahlungsempfänger, sonst der
  Zahlungspflichtige.
* **alt:** Vorspann ``"Kontonummer:";"DE…"``, ``"Von:"``, ``"Bis:"``, ``"Kontostand vom …"``; Kopf ``Buchungstag;Wertstellung;
  Buchungstext;Auftraggeber / Begünstigter;Verwendungszweck;Kontonummer;BLZ;Betrag (EUR);…``, Datum ``TT.MM.JJJJ``, oft
  Windows-1252, einmal mit Komma statt Semikolon und einmal mit schon beim Schreiben verlorenem Umlaut in „Begünstigter“ –
  deshalb erkennt keine Spalte sich an einem Umlaut. Die Gegenseite steht in einer Spalte, ihre Richtung gibt das Vorzeichen.

Die Kopfzeile ist die erste Zeile, deren erste Zelle ``Buchungsdatum`` oder ``Buchungstag`` heißt; ihr Trennzeichen
(Semikolon oder Komma) wird an ihr abgelesen, nicht an den Beträgen (jeder deutsche Betrag trägt ein Komma). Kodierung: BOM,
dann striktes UTF-8, dann Windows-1252 – nie Windows-1252 zuerst, es dekodiert fast alles.

Beträge liest ``money.parse_grouped_amount`` (der Punkt ist immer ein Tausendertrenner: die DKB lässt bei runden Beträgen die
Nachkommastellen weg, ``-15.000`` heißt minus fünfzehntausend), nie ``float()``. ``StatementTransaction.amount`` ist wie bei
CAMT ein auf den Cent gerundeter ``float``.

Befund-Schlüssel auf Beleg-Ebene (``BankStatement.warnings``):

====================  =======================================================================================
``unparsed``          eine Zeile ließ sich nicht sicher lesen (siehe unten): nicht importiert, kann fehlen
``skipped_status``    vorgemerkte Umsätze (Status „Vorgemerkt …“, nur im neuen Format) wurden übergangen
``no_balance``        immer: Die Datei nennt keinen Anfangssaldo, die Vollständigkeit ist nicht geprüft
====================  =======================================================================================

Hinzu kommt ``no_iban`` (die IBAN steht im Vorspann; fehlt sie, setzt ``plausibility.check_statement`` den Befund, und die
Vorschau zeigt „Konto nicht gefunden“).

Nicht sicher lesbar (``unparsed``) ist eine Zeile, der das Datum fehlt oder der lesbare Betrag – oder die verrutscht ist: mehr
Zellen als die Kopfzeile (ein Trennzeichen ohne Anführung im Text, ein Betrag ohne Anführung in einer Komma-Datei) oder zu
wenige für Betrag, Zweck und Gegenseite. In der Betragsspalte einer verrutschten Zeile steht etwas anderes, und das wird nie
als Betrag gelesen. Ein Datum vor ``MIN_YEAR`` oder mehr als ``MAX_DAYS_AHEAD`` Tage nach heute gilt als nicht lesbar. Still
übergangen wird nur eine Fußzeile: kein Datum UND nichts in der Betragsspalte.

Erkannt wird die Liste allein an ihrer Kopfzeile (erste Zelle ``Buchungsdatum``/``Buchungstag``, dazu je eine Spalte für
Betrag, Verwendungszweck und Gegenseite): Die Liste einer anderen Bank mit derselben Kopfzeile wird genauso gelesen. Nennt
der Name der Betragsspalte eine andere Währung als Euro („Betrag (USD)“), wird nichts importiert.

``document_date`` bleibt ``None``: Die Plausibilitätsprüfung (``daterange``) misst ein Buchungsdatum am Belegdatum und
meldet es, wenn es mehr als 400 Tage davor oder 31 Tage danach liegt. Bei einer Umsatzliste gibt es kein Belegdatum, das
dazu passte – sie umfasst einen beliebigen Zeitraum (ein Jahr, mehrere Jahre) und wird irgendwann danach exportiert („Kontostand
vom …“ ist der Tag des Exports) –, und der Jahresdreher, den die Prüfung sucht, kann bei einem Datum mit Jahr nicht
entstehen. Mit ``None`` entfällt die Prüfung ganz (``check_transaction`` prüft ``daterange`` nur mit Belegdatum); jede
andere Wahl (Exporttag, letztes Buchungsdatum, Ende des Zeitraums) meldete bei einem Jahresexport viele Zeilen zu Unrecht.

Die Datei ist eine Nutzereingabe: höchstens ``MAX_CSV_BYTES``, ``MAX_CSV_ROWS`` Zeilen und ``MAX_LINE_DELIMITERS``
Trennzeichen in einer Zeile, lineare Laufzeit, nur im
Speicher gelesen, nie auf die Platte geschrieben. Meldungen (``DkbCsvError``) sind feste Sätze und nennen keinen Feldwert der
Datei; Spaltennamen der Kopfzeile stehen höchstens gekürzt darin.
"""
import csv
import io
import re
from datetime import date, timedelta
from decimal import Decimal

from money import parse_grouped_amount

from .base import BankStatement, StatementTransaction

BANK_CODE = 'DKB'

#: Grenzen gegen überlange Eingaben (die Datei kommt aus einem Upload)
MAX_CSV_BYTES = 20 * 1024 * 1024    # Größe der Datei
MAX_CSV_ROWS = 100_000              # Zeilen hinter der Kopfzeile (auch leere und Fußzeilen)
MAX_PREAMBLE_ROWS = 100             # so weit vorn muss die Kopfzeile stehen (der Vorspann hat vier bis sieben Zeilen)
MAX_COLUMNS = 100                   # Spalten der Kopfzeile
MAX_LINE_DELIMITERS = 2000          # Trennzeichen in einer Zeile der Datei (eine Umsatzzeile hat rund ein Dutzend)
MIN_YEAR = 1990                     # früher gab es das Institut nicht – ein Jahr davor ist ein Lesefehler
MAX_DAYS_AHEAD = 31                 # ein gebuchter Umsatz liegt nicht in der Zukunft (Spielraum für eine falsch gehende Uhr)
MAX_NAME_LENGTH = 40                # Länge eines Spaltennamens in einer Meldung
MAX_NAMES_SHOWN = 30                # Zahl der Spaltennamen in einer Meldung

NOT_RECOGNISED = ('Die CSV-Datei wurde nicht als Kontoauszug erkannt (unterstützt wird die Umsatzliste der DKB) '
                  '– nichts importiert.')
TOO_BIG = f'Die CSV-Datei ist zu groß (höchstens {MAX_CSV_BYTES // (1024 * 1024)} MB) – nichts importiert.'
TOO_MANY_ROWS = ('Die CSV-Datei hat mehr als ' + f'{MAX_CSV_ROWS:,}'.replace(',', '.') + ' Zeilen – bitte einen kürzeren '
                 'Zeitraum exportieren. Nichts importiert.')
UNREADABLE = 'Die CSV-Datei ließ sich nicht lesen – nichts importiert.'
FOREIGN_CURRENCY = 'Die Umsatzliste nennt eine andere Währung als Euro – nichts importiert.'


class DkbCsvError(ValueError):
    """Die Datei ist keine lesbare DKB-Umsatzliste. Der Text ist ein fester Satz für den Nutzer und nennt nie einen Feldwert
    der Datei."""


#: Wie die erste Spalte der Tabelle heißt (neu / alt). Zwei Wörter, denn die Kopfzeile unterscheidet die Tabelle vom Vorspann.
DAY_COLUMNS = ('Buchungsdatum', 'Buchungstag')

#: Die Bedeutungen, je als die Wörter, die für sie standen, in dieser Reihenfolge gelesen – eine Umbenennung ergänzt hier ein
#: Wort und sonst nichts. ``beg`` ist der letzte Rückgriff für eine einzelne „Begünstigter“-Spalte, deren Umlaut verloren ging.
#: Bewusst eng statt großzügig: „Wertstellung“ enthält *wert* und „Umsatztyp“ *umsatz* – eine weite Liste läse das Wertstellungs-
#: datum als Betrag, und eine falsche Spalte ist schlimmer als keine, denn keine meldet sich.
MEANS = {
    'amount': ('betrag',),
    'purpose': ('verwendungszweck', 'zweck'),
    'payee': ('empf',),
    'payer': ('pflicht', 'zahler'),
    'party': ('auftraggeber', 'gegenseite', 'beg'),
    # gebucht oder vorgemerkt; nur der neue Export hat die Spalte, ohne sie ist alles gebucht
    'status': ('status',),
    # Konto der Gegenseite: der alte Export nennt es Kontonummer und führt dort eine IBAN oder bei alten Buchungen eine Nummer
    # aus der Zeit vor der IBAN
    'iban': ('iban', 'kontonummer'),
}

_CENT = Decimal('0.01')

# Die Währung, die der Name der Betragsspalte in Klammern nennt („Betrag (EUR)“) – nur ein Kürzel aus drei Buchstaben zählt
_CURRENCY_IN_NAME = re.compile(r'\(\s*([A-Za-z]{3})\s*\)')

# Datum der Datei: Tag und Monat ein- oder zweistellig, das Jahr zwei- (neu) oder vierstellig (alt); nur ASCII-Ziffern
_DATE = re.compile(r'([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4}|[0-9]{2})')

# Eine ganze IBAN: zwei Buchstaben, zwei Prüfziffern, elf bis dreißig Buchstaben oder Ziffern (15 bis 34 Zeichen)
_WHOLE_IBAN = re.compile(r'[A-Z]{2}[0-9]{2}[0-9A-Z]{11,30}')
# Eine IBAN irgendwo in einem Text des Vorspanns, mit oder ohne Leerzeichen in Vierergruppen
_IBAN_IN_TEXT = re.compile(r'\b[A-Z]{2}[0-9]{2}(?:\s?[0-9A-Z]{4}){3,7}(?:\s?[0-9A-Z]{1,3})?\b')


def _decoded(raw: bytes) -> str:
    """Der Text einer Datei, in welcher Kodierung die Bank ihn auch schrieb: BOM, striktes UTF-8, Windows-1252. Fällt auch das
    durch (Windows-1252 kennt fünf Bytes nicht), wird UTF-8 mit Ersatzzeichen gelesen – die Kopfzeile ist ASCII, und keine
    Spalte erkennt sich an einem Umlaut, also ändert der Rückgriff nichts an der Erkennung."""
    for encoding in ('utf-8-sig', 'cp1252'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def _delimiter(text: str):
    """Semikolon oder Komma, an der Kopfzeile selbst abgelesen – nicht an der ganzen Datei: Jeder deutsche Betrag trägt ein
    Komma, und gezählt über die Datei sähe ein Semikolon-Export wie ein Komma-Export aus. ``None``, wenn die ersten Zeilen
    keine Kopfzeile haben."""
    for number, line in enumerate(io.StringIO(text, newline='')):
        if number >= MAX_PREAMBLE_ROWS:
            break
        if line.lstrip('﻿" \t').startswith(DAY_COLUMNS):
            return ';' if line.count(';') >= line.count(',') else ','
    return None


def _column(meaning: str, lowered_names):
    """Index der Spalte mit dieser Bedeutung oder ``None``. Nichts wird am genauen Namen erkannt."""
    for word in MEANS[meaning]:
        for index, name in enumerate(lowered_names):
            if word in name:
                return index
    return None


def _cell(row, index) -> str:
    return row[index] if index is not None and index < len(row) else ''


def _squeeze(text: str) -> str:
    """Leerraum zu einem Leerzeichen zusammengezogen, außen ohne."""
    return ' '.join(text.split())


def _iso_date(text: str):
    """``TT.MM.JJ`` und ``TT.MM.JJJJ`` als ``JJJJ-MM-TT`` – oder ``None``, wo es kein Datum gibt (auch 31.02.) oder keines,
    an dem die Bank gebucht haben kann: vor ``MIN_YEAR`` (``05.01.0024``) oder mehr als ``MAX_DAYS_AHEAD`` Tage nach heute
    (``31.12.99``). Ein zweistelliges Jahr gehört ins 21. Jahrhundert (die DKB schreibt es erst seit 2023)."""
    found = _DATE.fullmatch(text.strip())
    if found is None:
        return None
    day, month, year = int(found.group(1)), int(found.group(2)), int(found.group(3))
    if len(found.group(3)) == 2:
        year += 2000
    if year < MIN_YEAR:
        return None
    try:
        booked = date(year, month, day)
    except ValueError:
        return None
    if booked > date.today() + timedelta(days=MAX_DAYS_AHEAD):
        return None
    return booked.isoformat()


def _iban_in(text: str) -> str:
    """Die IBAN, die ein Feld ganz ausmacht (ohne Leerzeichen, groß) – sonst ``''``. Eine Kontonummer aus der Zeit vor der
    IBAN oder ein leeres Feld nennt kein Konto, das sich von einem anderen unterscheiden ließe."""
    compact = ''.join(text.split()).upper()
    return compact if _WHOLE_IBAN.fullmatch(compact) else ''


def _iban_of_row(row) -> str:
    """Die IBAN, die eine Zeile des Vorspanns nennt – als ganzes Feld oder irgendwo in einem Text; sonst ``''``."""
    for cell in row:
        found = _iban_in(cell)
        if not found:
            hit = _IBAN_IN_TEXT.search(cell.upper())
            found = _iban_in(hit.group()) if hit else ''
        if found:
            return found
    return ''


def _names_text(names) -> str:
    shown = [name[:MAX_NAME_LENGTH] for name in names if name][:MAX_NAMES_SHOWN]
    return ', '.join(shown) + (' …' if len([n for n in names if n]) > len(shown) else '')


def _statement(reader) -> BankStatement:
    """Vorspann, Kopfzeile und Zeilen aus einem ``csv.reader`` – lazy, in einem Durchlauf."""
    iban = ''
    header = None
    for number, row in enumerate(reader):
        if row and row[0].strip().lstrip('﻿') in DAY_COLUMNS:
            header = row
            break
        if number >= MAX_PREAMBLE_ROWS:
            break
        iban = iban or _iban_of_row(row[:MAX_COLUMNS])
    if header is None or len(header) > MAX_COLUMNS:
        raise DkbCsvError(NOT_RECOGNISED)

    names = [_squeeze(cell).lstrip('﻿') for cell in header]
    lowered = [name.lower() for name in names]
    column = {meaning: _column(meaning, lowered) for meaning in MEANS}
    two_sides = column['payee'] is not None and column['payer'] is not None
    # Nur EINE der beiden Seiten ist keine Gegenseite: Jede Gutschrift (oder jede Belastung) trüge den falschen Namen – auch
    # dann, wenn daneben eine einzelne Spalte stünde, die sich als Gegenseite lesen ließe
    one_side = (column['payee'] is None) != (column['payer'] is None)
    party_found = two_sides or (column['party'] is not None and not one_side)
    if column['amount'] is None or column['purpose'] is None or not party_found:
        # Die fehlende Bedeutung wird genannt – der nächste Formatwechsel landet hier, und die Frage ist, welche Spalte fehlt
        missing = [what for what, found in (('Betrag', column['amount'] is not None),
                                            ('Verwendungszweck', column['purpose'] is not None),
                                            ('Gegenseite (Empfänger/Auftraggeber)', party_found)) if not found]
        raise DkbCsvError(f"Der Umsatzliste fehlt die Spalte für {' und '.join(missing)}. "
                          f'Gefunden wurden: {_names_text(names)}. Nichts importiert.')
    currency = _CURRENCY_IN_NAME.search(names[column['amount']])
    if currency and currency.group(1).upper() != 'EUR':
        raise DkbCsvError(FOREIGN_CURRENCY)
    # So weit muss eine Zeile reichen, damit Betrag, Zweck und Gegenseite in ihr stehen
    needed = max(index for index in (column['amount'], column['purpose'], column['party'] if not two_sides else None,
                                     column['payee'] if two_sides else None, column['payer'] if two_sides else None)
                 if index is not None)

    transactions = []
    unparsed = skipped_status = False
    count = 0
    for row in reader:
        count += 1
        if count > MAX_CSV_ROWS:
            raise DkbCsvError(TOO_MANY_ROWS)
        if not row:
            continue
        # Eine vorgemerkte Buchung wird später gebucht, oft mit anderem Tag oder Wortlaut – unter anderem Schlüssel; jetzt
        # übernommen stünde sie doppelt da
        if _cell(row, column['status']).strip().lower().startswith('vorgemerkt'):
            skipped_status = True
            continue
        day = _iso_date(_cell(row, 0))
        amount_text = _cell(row, column['amount'])
        # Eine Zeile mit mehr Zellen als die Kopfzeile (ein Trennzeichen ohne Anführung im Text, ein Betrag ohne Anführung
        # in einer Komma-Datei) oder zu wenigen ist verrutscht: In der Betragsspalte steht dann etwas anderes – eine
        # Bankleitzahl läse sich als Millionenbetrag. Ihr Betrag gilt als nicht lesbar
        shifted = len(row) > len(header) or len(row) <= needed
        amount = None if shifted else parse_grouped_amount(amount_text)
        if day is None and not amount_text.strip():
            continue                                  # Fußzeile: weder Datum noch etwas in der Betragsspalte
        if day is None or amount is None:
            unparsed = True                           # nicht raten: nicht importiert, und der Befund sagt es
            continue
        # Bei einer Belastung steht die Gegenseite beim Zahlungsempfänger, bei einer Gutschrift beim Zahlungspflichtigen –
        # sonst trüge jede Erstattung den eigenen Namen
        recipient = (_cell(row, column['payee'] if amount < 0 else column['payer']) if two_sides
                     else _cell(row, column['party']))
        transactions.append(StatementTransaction(
            date=day,
            amount=float(amount.quantize(_CENT)),
            recipient=_squeeze(recipient),
            reference=_squeeze(_cell(row, column['purpose'])),
            foreign_iban=_iban_in(_cell(row, column['iban'])),
        ))

    warnings = [key for key, present in (('unparsed', unparsed), ('skipped_status', skipped_status)) if present]
    warnings.append('no_balance')                     # die Datei nennt keinen Anfangssaldo: nichts zum Vergleichen
    return BankStatement(bank_code=BANK_CODE, iban=iban or None, document_date=None,
                         transactions=transactions, warnings=warnings)


def _lines(text: str, delimiter: str):
    """Die Zeilen der Datei für den ``csv.reader`` – eine mit absurd vielen Trennzeichen bricht ab, bevor aus ihr eine Liste
    mit Millionen Zellen wird (das Modul begrenzt die Länge eines Feldes, nicht ihre Zahl)."""
    for line in io.StringIO(text, newline=''):
        if line.count(delimiter) > MAX_LINE_DELIMITERS:
            raise DkbCsvError(UNREADABLE)
        yield line


def parse_csv(data: bytes) -> BankStatement:
    """Eine DKB-Umsatzliste (Bytes) lesen – nur im Speicher.

    Raises :class:`DkbCsvError` mit festem Klartext, wenn die Datei zu groß ist, keine Kopfzeile hat (also keine
    DKB-Umsatzliste ist), eine Spalte für Betrag, Verwendungszweck oder Gegenseite fehlt, eine andere Währung als Euro
    genannt ist, mehr als ``MAX_CSV_ROWS`` Zeilen hat oder sich
    nicht als CSV lesen lässt."""
    if len(data) > MAX_CSV_BYTES:
        raise DkbCsvError(TOO_BIG)
    text = _decoded(data)
    delimiter = _delimiter(text)
    if delimiter is None:
        raise DkbCsvError(NOT_RECOGNISED)
    try:
        return _statement(csv.reader(_lines(text, delimiter), delimiter=delimiter))
    except csv.Error:
        # etwa ein Feld über der Standardgrenze des csv-Moduls (131072 Zeichen), das eine offene Anführung verschluckt
        raise DkbCsvError(UNREADABLE) from None
