# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zu einem Zahlungseingang die offene Rechnung vorschlagen – und zu einer Rechnung die Zahlungseingänge: die reine Rangfolge (seit 2.8.1).

Rein: kein Datenbankzugriff, keine Uhr. Die Zeilen liefert ``db/payment_suggestions.py`` (``Database.suggest_invoice_payments``,
``suggest_invoices_for_booking``, ``suggest_bookings_for_invoice``). Vorgeschlagen wird nur: Zugeordnet wird allein auf Klick, über den
bestehenden Weg (``Database.assign_invoice_payment``) – hier wird nichts geschrieben, nichts entschieden und nichts vorausgewählt.

Eingabe sind Wörterbücher. Eine Bewegung: ``id``, ``date``, ``amount_minor`` (der FREIE Rest der Bankbewegung in Minor Units, größer 0),
``recipient``, ``text``, ``document_number`` (Beleg-Nr.) und ``contact_id``. Eine Rechnung: ``id``, ``number``, ``date``, ``open_minor`` (der
offene Betrag in Minor Units, größer 0), ``customer_id`` und ``customer_name``. Eine Zeile, deren ``id`` oder Betrag keine ganze Zahl (beim Betrag
größer 0) ist, ist kein Kandidat; ein schiefer Text, ein fehlender Schlüssel und ein unlesbares Datum werfen nie – das Signal fällt weg.

Signale je Paar (Bewegung, Rechnung), je eines mit festem Satz als Begründung und Punkten:

* **NUMMER** (``REASON_NUMBER``, ``POINTS_NUMBER``): Die Rechnungsnummer steht im Verwendungszweck, in der Beleg-Nr. oder im Empfänger der
  Bewegung (``_number_pattern``). Brauchbar ist eine Nummer wie bei den Belegen (``receipt_matching.usable_number``: normalisiert mindestens 4
  Zeichen mit einer Ziffer, rein numerisch erst ab 5) und höchstens ``MAX_NUMBER_LENGTH`` Zeichen lang.
* **BETRAG** (``REASON_AMOUNT``, ``POINTS_AMOUNT``): Der freie Rest der Bewegung ist der offene Betrag der Rechnung, auf den Minor-Wert.
* **KUNDE** (``REASON_CUSTOMER``, ``POINTS_CUSTOMER``): Der Kontakt der Bewegung ist der Kunde der Rechnung, oder Empfänger und Kundenname haben
  mindestens ein Namenswort gemeinsam (``receipt_matching.name_words``: ohne ``STOP_WORDS``, mindestens 4 Zeichen).
* **DATUM** zählt nur für die Reihenfolge (Abstand in Tagen zwischen Buchungs- und Rechnungsdatum; ein unlesbares Datum steht ganz hinten) und
  für eine Ausnahme: Liegt die Bewegung mehr als ``DAYS_BEFORE_INVOICE`` Tage VOR dem Rechnungsdatum, ist sie ohne NUMMER kein Vorschlag (mit
  NUMMER schon: eine Vorauszahlung).

**Ein Paar ist ein Vorschlag, wenn NUMMER zutrifft oder BETRAG und KUNDE zutreffen** – ein gleicher Betrag allein beweist nichts. Die Begründungen
stehen in fester Reihenfolge (Nummer, Betrag, Kunde); trifft NUMMER zu und BETRAG nicht, kommt ``REASON_PARTIAL`` dazu (eine Teil- oder Überzahlung
ist dann möglich). ``rank_invoices`` und ``rank_movements`` bringen die Paare EINER Bewegung bzw. EINER Rechnung in eine Rangfolge (höchstens
``MAX_SUGGESTIONS``); ``pair_up`` macht daraus die Eins-zu-eins-Liste für die Übersicht – jede Bewegung und jede Rechnung höchstens einmal, die
zeitlich nächste zuerst, deterministisch. Schon zugeordnete Paare (``linked``) sind nie ein Vorschlag.

Laufzeit: je Paar mit brauchbarer Nummer ein billiger Test (die normalisierte Nummer steht in den normalisierten Feldern der Bewegung, ein ``in``);
nur bei einem Treffer läuft der genaue reguläre Ausdruck, und der ist linear. Die Paare aus Betrag und Kunde entstehen über einen Index nach dem
offenen Betrag, nicht über alle Paare."""
import dataclasses
import functools
import heapq
import re
from collections import Counter
from typing import Iterable, Iterator

from .receipt_matching import is_int, iso_day, name_words, normalize_number, text_field, usable_number

MAX_SUGGESTIONS = 5              # so viele Vorschläge liefern ``rank_invoices`` und ``rank_movements`` höchstens
MAX_NUMBER_LENGTH = 40           # eine längere (normalisierte) Rechnungsnummer ist keine Nummer, sondern ein Text: kein Treffer
DAYS_BEFORE_INVOICE = 7          # so viele Tage vor dem Rechnungsdatum darf eine Zahlung liegen, ohne dass die Nummer sie belegt

POINTS_NUMBER = 100
POINTS_AMOUNT = 50
POINTS_CUSTOMER = 30

REASON_NUMBER = 'Rechnungsnummer im Verwendungszweck'
REASON_AMOUNT = 'Betrag stimmt'
REASON_CUSTOMER = 'Kunde passt'
REASON_PARTIAL = 'Betrag weicht ab (Teil- oder Überzahlung)'
AMBIGUOUS_TEXT = 'mehrere möglich – bitte prüfen'        # der Zusatz einer Zeile der Übersicht, die ``pair_up`` als mehrdeutig kennzeichnet
REASON_SEPARATOR = ' · '

# Zwischen zwei Zeichen der Rechnungsnummer darf höchstens EIN Trennzeichen stehen: Leerraum (auch ein Zeilenumbruch: Banken brechen
# Zeilen um, ein Kunde schreibt ``R 26003`` oder ``R-26003``), Bindestrich, Unterstrich, Schrägstrich, Punkt.
_GAP = r'[\s\-_/.]?'
_NO_DISTANCE = 10 ** 9           # ein Paar ohne lesbares Datum steht in der Reihenfolge ganz hinten


@dataclasses.dataclass(frozen=True)
class PaymentSuggestion:
    """Ein Vorschlag: die Bewegung, die Rechnung, die Punkte, die Begründungen (feste Sätze: Nummer, Betrag, Kunde), der Abstand in Tagen
    (Buchungsdatum minus Rechnungsdatum, negativ = davor; ``None``, wenn eines der beiden Daten fehlt oder kein ISO-Datum ist) und – nur in
    der Liste von ``pair_up`` – ob es für diese Bewegung ODER diese Rechnung ein weiteres Paar mit derselben Punktzahl gab (``ambiguous``)."""
    movement_id: int
    invoice_id: int
    score: int
    reasons: tuple[str, ...]
    day_distance: int | None
    ambiguous: bool = False


def reason_text(suggestion: PaymentSuggestion) -> str:
    """Die Begründung zum Anzeigen: die festen Sätze mit ``REASON_SEPARATOR`` verbunden, bei einem mehrdeutigen Vorschlag mit dem Zusatz
    ``AMBIGUOUS_TEXT`` am Ende. EINE Stelle für den Kasten der Übersicht, die Maske und die Rechnungsseite."""
    return REASON_SEPARATOR.join(suggestion.reasons + ((AMBIGUOUS_TEXT,) if suggestion.ambiguous else ()))


@functools.lru_cache(maxsize=4096)
def _number_pattern(number: str) -> re.Pattern:
    """Der Ausdruck für eine normalisierte Rechnungsnummer (``number``: nicht leer, nur ``[a-z0-9]``): ihre Zeichen in dieser Reihenfolge, zwischen je
    zwei höchstens ein Trennzeichen (``_GAP``), ohne Rücksicht auf die Schreibweise – gesucht wird im Originaltext, nicht im normalisierten
    (in ``26re2026004211`` steckt ``re20260042`` als Teilzeichenkette, aber nicht als Nummer: Es folgen weitere Ziffern).

    **Die Ziffernsperre** gilt an dem Rand, an dem die Nummer selbst mit einer Ziffer beginnt bzw. endet: Dort steht unmittelbar keine weitere
    ASCII-Ziffer, sonst wäre der Treffer nur das Stück einer längeren Ziffernfolge – eines Betrags, eines Datums, einer anderen Nummer (``R26001`` ist
    nicht in ``R260010``, ``20260042`` nicht in ``120260042``). An einem Rand, an dem die Nummer mit einem Buchstaben beginnt oder endet, gibt es keine
    Sperre: ``RE-Nr.R26002.KD-Nr.``, ``R26003 12.11.2026`` und ``RGNR26003`` treffen, und auch ``1R26001`` trifft ``R26001`` – dort verlängert die
    Ziffer keine Ziffernfolge der Nummer. Ein Buchstabe oder ein Satzzeichen davor oder dahinter stört nie.

    Linear: Die Zeichen der Nummer sind ``[a-z0-9]`` (mit ``IGNORECASE`` auch groß), das Trennzeichen nie eines davon – an jeder Stelle des Textes
    passt höchstens EIN Weg. Es gibt keine verschachtelten Quantoren, und der Aufwand je Fundstelle hängt nur von der Länge der Nummer ab (höchstens
    ``MAX_NUMBER_LENGTH``)."""
    body = _GAP.join(re.escape(char) for char in number)
    before = r'(?<![0-9])' if number[0].isdigit() else ''
    after = r'(?![0-9])' if number[-1].isdigit() else ''
    return re.compile(before + body + after, re.IGNORECASE)


class _Movement:
    """Eine Bewegung, einmal für alle Paare vorbereitet: die gekappten Felder (Text, Beleg-Nr., Empfänger), ihre normalisierte Form, deren
    Verbindung (``haystack``: ``|`` ist kein Zeichen einer Nummer, über die Grenze eines Feldes hinweg trifft also nichts) und die Namenswörter
    des Empfängers."""
    __slots__ = ('id', 'amount', 'contact_id', 'day', 'fields', 'normalized', 'haystack', 'words')

    def __init__(self, raw: dict):
        self.id = raw['id']
        self.amount = raw['amount_minor']
        contact = raw.get('contact_id')
        self.contact_id = contact if is_int(contact) else None
        self.day = iso_day(raw.get('date'))
        self.fields = tuple(text_field(raw, key) for key in ('text', 'document_number', 'recipient'))
        self.normalized = tuple(normalize_number(field) for field in self.fields)
        self.haystack = '|'.join(self.normalized)
        self.words = name_words(self.fields[2])


class _Invoice:
    """Eine Rechnung, einmal für alle Paare vorbereitet: die normalisierte Nummer (``''``, wenn sie nicht brauchbar ist) und die Namenswörter
    des Kunden."""
    __slots__ = ('id', 'number', 'open', 'customer_id', 'day', 'words')

    def __init__(self, raw: dict):
        self.id = raw['id']
        self.open = raw['open_minor']
        customer = raw.get('customer_id')
        self.customer_id = customer if is_int(customer) else None
        self.day = iso_day(raw.get('date'))
        number = normalize_number(raw.get('number'))
        self.number = number if usable_number(number) and len(number) <= MAX_NUMBER_LENGTH else ''
        self.words = name_words(text_field(raw, 'customer_name'))


def _movements(raw: Iterable | None) -> list[_Movement]:
    return [_Movement(row) for row in raw or ()
            if isinstance(row, dict) and is_int(row.get('id')) and is_int(row.get('amount_minor')) and row['amount_minor'] > 0]


def _invoices(raw: Iterable | None) -> list[_Invoice]:
    return [_Invoice(row) for row in raw or ()
            if isinstance(row, dict) and is_int(row.get('id')) and is_int(row.get('open_minor')) and row['open_minor'] > 0]


def _number_found(movement: _Movement, invoice: _Invoice) -> bool:
    """Steht die Nummer der Rechnung in einem Feld der Bewegung – der genaue Ausdruck, je Feld mit Treffer im normalisierten Text. Der billige Test
    auf allen Feldern zusammen (``invoice.number in movement.haystack``) macht der Aufrufer vorher."""
    pattern = _number_pattern(invoice.number)
    return any(invoice.number in normalized and pattern.search(field) is not None
               for field, normalized in zip(movement.fields, movement.normalized))


def _rate(movement: _Movement, invoice: _Invoice, by_number: bool) -> PaymentSuggestion | None:
    """Das Paar bewerten – ``None``, wenn es kein Vorschlag ist (weder NUMMER noch BETRAG und KUNDE, oder eine Bewegung mehr als
    ``DAYS_BEFORE_INVOICE`` Tage vor dem Rechnungsdatum ohne NUMMER). ``by_number``: Die Nummer steht im Text (``_number_found``)."""
    amount = movement.amount == invoice.open
    customer = ((movement.contact_id is not None and movement.contact_id == invoice.customer_id)
                or not movement.words.isdisjoint(invoice.words))
    if not (by_number or (amount and customer)):
        return None
    distance = None
    if movement.day is not None and invoice.day is not None:
        distance = (movement.day - invoice.day).days
        if distance < -DAYS_BEFORE_INVOICE and not by_number:
            return None
    score, reasons = 0, []
    if by_number:
        score += POINTS_NUMBER
        reasons.append(REASON_NUMBER)
    if amount:
        score += POINTS_AMOUNT
        reasons.append(REASON_AMOUNT)
    if customer:
        score += POINTS_CUSTOMER
        reasons.append(REASON_CUSTOMER)
    if by_number and not amount:
        reasons.append(REASON_PARTIAL)
    return PaymentSuggestion(movement.id, invoice.id, score, tuple(reasons), distance)


def _pairs(movements: list[_Movement], invoices: list[_Invoice], linked) -> Iterator[PaymentSuggestion]:
    """Alle Vorschläge zwischen diesen Bewegungen und Rechnungen, ungeordnet. ``linked``: Paare ``(ID der Bewegung, ID der Rechnung)``, die schon
    zugeordnet sind – nie ein Vorschlag. Die Paare mit Nummer kommen aus dem billigen Test über alle Rechnungen mit brauchbarer Nummer, die mit
    Betrag und Kunde aus einem Index nach dem offenen Betrag (nicht aus allen Paaren)."""
    numbered = [(invoice.number, invoice) for invoice in invoices if invoice.number]
    by_amount: dict[int, list[_Invoice]] = {}
    for invoice in invoices:
        by_amount.setdefault(invoice.open, []).append(invoice)
    for movement in movements:
        haystack, by_number = movement.haystack, set()
        for number, invoice in numbered:
            if number in haystack and (movement.id, invoice.id) not in linked and _number_found(movement, invoice):
                by_number.add(id(invoice))
                yield _rate(movement, invoice, True)
        for invoice in by_amount.get(movement.amount, ()):
            if id(invoice) not in by_number and (movement.id, invoice.id) not in linked:
                suggestion = _rate(movement, invoice, False)
                if suggestion is not None:
                    yield suggestion


def _distance(suggestion: PaymentSuggestion) -> int:
    return _NO_DISTANCE if suggestion.day_distance is None else abs(suggestion.day_distance)


def rank_invoices(movement: dict, invoices: Iterable[dict], linked=frozenset()) -> list[PaymentSuggestion]:
    """Die offenen Rechnungen, die zu EINER Bewegung passen: höchstens ``MAX_SUGGESTIONS`` Vorschläge nach ``(−Punkte, Abstand der Daten, ID der
    Rechnung)``. ``linked``: die schon zugeordneten Paare ``(ID der Bewegung, ID der Rechnung)``."""
    return heapq.nsmallest(MAX_SUGGESTIONS, _pairs(_movements([movement]), _invoices(invoices), linked),
                           key=lambda suggestion: (-suggestion.score, _distance(suggestion), suggestion.invoice_id))


def rank_movements(invoice: dict, movements: Iterable[dict], linked=frozenset()) -> list[PaymentSuggestion]:
    """Die Bewegungen, die zu EINER Rechnung passen: höchstens ``MAX_SUGGESTIONS`` Vorschläge nach ``(−Punkte, Abstand der Daten, ID der
    Bewegung)``. ``linked``: wie bei ``rank_invoices``."""
    return heapq.nsmallest(MAX_SUGGESTIONS, _pairs(_movements(movements), _invoices([invoice]), linked),
                           key=lambda suggestion: (-suggestion.score, _distance(suggestion), suggestion.movement_id))


def pair_up(movements: Iterable[dict], invoices: Iterable[dict], linked=frozenset()) -> list[PaymentSuggestion]:
    """Die Eins-zu-eins-Liste für die Übersicht: Alle Vorschlagspaare werden nach ``(−Punkte, Abstand der Daten, ID der Bewegung, ID der
    Rechnung)`` geordnet und der Reihe nach genommen, solange weder die Bewegung noch die Rechnung schon vergeben ist. Bei einem Kunden mit zwei
    gleich hohen Rechnungen und zwei gleich hohen Zahlungen (ohne Nummer im Text) bekommt so jede Rechnung die zeitlich nächste Zahlung –
    deterministisch, ohne Zufall. Jedes Ergebnis trägt ``ambiguous``: Es gab für diese Bewegung ODER diese Rechnung ein weiteres Paar (auch eines, das
    wegen einer schon vergebenen Bewegung oder Rechnung nicht in die Liste kam) mit derselben Punktzahl. ``linked``: wie bei ``rank_invoices``."""
    pairs = sorted(_pairs(_movements(movements), _invoices(invoices), linked),
                   key=lambda suggestion: (-suggestion.score, _distance(suggestion), suggestion.movement_id, suggestion.invoice_id))
    per_movement = Counter((suggestion.movement_id, suggestion.score) for suggestion in pairs)
    per_invoice = Counter((suggestion.invoice_id, suggestion.score) for suggestion in pairs)
    taken_movements, taken_invoices, result = set(), set(), []
    for suggestion in pairs:
        if suggestion.movement_id in taken_movements or suggestion.invoice_id in taken_invoices:
            continue
        taken_movements.add(suggestion.movement_id)
        taken_invoices.add(suggestion.invoice_id)
        if (per_movement[(suggestion.movement_id, suggestion.score)] > 1
                or per_invoice[(suggestion.invoice_id, suggestion.score)] > 1):
            suggestion = dataclasses.replace(suggestion, ambiguous=True)
        result.append(suggestion)
    return result
