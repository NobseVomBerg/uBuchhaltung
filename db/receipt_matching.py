# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zu einem gelesenen Beleg die passenden Buchungen vorschlagen – die reine Rangfolge (seit 2.6.0).

Rein: kein Datenbankzugriff, keine Uhr. Die Kandidaten liefert ``Database.receipt_match_candidates`` (``db/document_readings.py``),
``Database.suggest_bookings_for_receipt`` setzt beides zusammen. Vorgeschlagen wird nur; verknüpft wird nichts.

Ein Kandidat kommt in Frage, wenn sein Betrag ohne Vorzeichen der Bruttobetrag der Lesung ist (Minor Units, ``abs(amount) ==
abs(GrossMinor)``): Eine Ausgabe von −119,00 € passt zum Beleg über 119,00 €. Das allein ist kein Treffer, nur die Voraussetzung –
Punkte (und je eine Begründung als fester Satz) gibt es für:

* die Rechnungsnummer des Belegs im Buchungstext, in der Beleg-Nr. oder im Empfänger der Buchung (``POINTS_NUMBER``) – normalisiert
  mindestens ``MIN_NUMBER_LENGTH`` Zeichen mit einer Ziffer, eine rein numerische erst ab ``MIN_NUMERIC_NUMBER_LENGTH`` (``2026``
  träfe sonst jede Jahreszahl im Verwendungszweck),
* gemeinsame Namenswörter von Beleg und Buchung (``POINTS_NAME_ONE``, ab zwei ``POINTS_NAME_SEVERAL``),
* ein Buchungsdatum nahe dem Belegdatum, im Fenster von ``WINDOW_DAYS_BEFORE`` Tagen davor bis ``WINDOW_DAYS_AFTER`` Tagen danach
  (bis ``POINTS_DATE``, je ``DATE_STEP_DAYS`` Tage Abstand einer weniger).

Die Belegart bestimmt die erwartete Zahlungsrichtung: Eine Rechnung (oder ein Beleg ohne Art) wird bezahlt – erwartet wird ein Ausgang
(Betrag < 0) –, bei einer Gutschrift kommt Geld herein (Betrag > 0). Passt die Richtung nicht, trägt der Vorschlag die Begründung
``REASON_DIRECTION`` und steht hinter denen mit passender Richtung; ausgeschlossen wird er nicht.

Eine Buchung, die schon einen Beleg trägt, steht immer hinter allen anderen. Gerechnet wird in einem Durchgang: Wortmenge und
Rechnungsnummer des Belegs entstehen einmal, je Kandidat wird nur dessen (gekappter) Text gelesen."""
import datetime
import heapq
import re
from dataclasses import dataclass

WINDOW_DAYS_BEFORE = 7          # so viele Tage vor dem Belegdatum beginnt das Fenster der Kandidaten
WINDOW_DAYS_AFTER = 90          # … und so viele Tage danach endet es
MAX_SUGGESTIONS = 5             # so viele Vorschläge liefert ``rank_candidates`` höchstens
MAX_CANDIDATES = 200            # so viele Kandidaten liest die Abfrage höchstens
MIN_NUMBER_LENGTH = 4           # eine kürzere Rechnungsnummer (normalisiert) beweist nichts
MIN_NUMERIC_NUMBER_LENGTH = 5   # … eine rein numerische erst ab so vielen Ziffern (``2026`` steht in jedem Text mit einer Jahreszahl)
MIN_WORD_LENGTH = 4             # kürzere Wörter zählen nicht als Name
MAX_FIELD_LENGTH = 2000         # Text, Beleg-Nr., Empfänger und Name einer Buchung werden vor dem Lesen so weit gekappt

POINTS_NUMBER = 100
POINTS_NAME_ONE = 30
POINTS_NAME_SEVERAL = 40
POINTS_DATE = 20                # am Belegdatum; je ``DATE_STEP_DAYS`` Tage Abstand einer weniger, nie unter 0
DATE_STEP_DAYS = 3

REASON_AMOUNT = 'Betrag stimmt'
REASON_NUMBER = 'Rechnungsnummer im Buchungstext'
REASON_NAME = 'Name passt'
REASON_SAME_DAY = 'am Belegdatum'
REASON_DAY_AFTER = '1 Tag nach dem Belegdatum'
REASON_DAYS_AFTER = '{days} Tage nach dem Belegdatum'
REASON_DAY_BEFORE = '1 Tag vor dem Belegdatum'
REASON_DAYS_BEFORE = '{days} Tage vor dem Belegdatum'
REASON_DIRECTION = 'Zahlungsrichtung passt nicht zur Belegart'
REASON_LINKED = 'bereits mit einem Beleg verknüpft'

#: Wörter, die auf Belegen und in Buchungstexten stehen, ohne einen Namen zu tragen (kleingeschrieben und ohne ß: so, wie
#: ``name_words`` sie nach ``casefold`` vergleicht). Kürzere als ``MIN_WORD_LENGTH`` Zeichen fallen ohnehin weg.
STOP_WORDS = frozenset({
    # Rechtsformen
    'gmbh', 'mbh', 'ohg', 'gbr', 'inc', 'ltd', 'llc', 'kgaa', 'gesellschaft', 'haftungsbeschränkt',
    # Belege und Buchungen
    'rechnung', 'rechnungen', 'rechnungsnummer', 'rechnungsnr', 'rechnungsdatum', 'beleg', 'belegnummer', 'kunde', 'kundennummer',
    'kundennr', 'betrag', 'datum', 'zahlung', 'zahlungen', 'lastschrift', 'basislastschrift', 'überweisung', 'ueberweisung',
    'gutschrift', 'dauerauftrag', 'verwendungszweck', 'buchung', 'referenz', 'mandatsreferenz', 'sepa', 'iban', 'bic', 'eref',
    'kref', 'mref', 'svwz', 'invoice', 'payment', 'amount',
    # Höflichkeitsfloskeln
    'vielen', 'dank', 'danke', 'bitte', 'freundlichen', 'grüssen',
})

_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')      # nur ASCII-Ziffern: ``\d`` träfe auch andere Schriften
_NOT_ALPHANUMERIC = re.compile(r'[^a-z0-9]+')
_WORD = re.compile(r'[^\W_]+')                              # Buchstaben und Ziffern (auch Umlaute), ohne ``_``
_NO_DISTANCE = 10 ** 9                                      # Abstand einer Buchung ohne lesbares Datum: ganz hinten


@dataclass(frozen=True)
class Suggestion:
    """Ein Vorschlag: die ID der Buchung, ihre Punkte, die Begründungen (feste Sätze, der Betrag zuerst), ob sie schon einen Beleg
    trägt, und ihr Abstand zum Belegdatum in Tagen (Buchungsdatum minus Belegdatum, negativ = davor; auch außerhalb des
    Fensters – ``None``, wenn eines der beiden Daten fehlt oder kein ISO-Datum ist). ``mismatch``: Die Zahlungsrichtung der Buchung
    passt nicht zur Belegart (``REASON_DIRECTION``) – nur für die Reihenfolge, nicht Teil der JSON-Form der Seite."""
    booking_id: int
    score: int
    reasons: tuple[str, ...]
    linked: bool
    day_distance: int | None
    mismatch: bool = False


def _text(value) -> str:
    return '' if value is None else value if isinstance(value, str) else str(value)


def is_int(value) -> bool:
    """Eine ganze Zahl (ein ``bool`` zählt nicht). Öffentlich, weil auch ``payment_matching`` sie nutzt."""
    return isinstance(value, int) and not isinstance(value, bool)


def iso_day(value) -> datetime.date | None:
    """Der Kalendertag, wenn ``value`` ein ISO-Datum ``JJJJ-MM-TT`` ist (ein Tag, den es gibt, Jahr 1900–2200, nichts davor oder
    dahinter) – sonst ``None``. Dieselbe Regel wie ``server.period.parse_iso_date``; die Datenbankschicht lädt das Server-Package nicht.
    Öffentlich, weil auch ``payment_matching`` sie nutzt."""
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        day = datetime.date.fromisoformat(value)
    except ValueError:
        return None
    return day if 1900 <= day.year <= 2200 else None


def candidate_window(doc_date: str | None) -> tuple[str, str] | None:
    """Das Fenster, in dem Buchungen zu einem Beleg mit diesem Datum gesucht werden: ``(von, bis)`` als ISO-Daten, ``WINDOW_DAYS_BEFORE``
    Tage vor bis ``WINDOW_DAYS_AFTER`` Tage nach ``doc_date`` (beide Tage gehören dazu). ``None``, wenn ``doc_date`` kein ISO-Datum ist."""
    day = iso_day(doc_date)
    if day is None:
        return None
    return ((day - datetime.timedelta(days=WINDOW_DAYS_BEFORE)).isoformat(),
            (day + datetime.timedelta(days=WINDOW_DAYS_AFTER)).isoformat())


def normalize_number(text) -> str:
    """Eine Nummer zum Vergleichen: ``casefold`` und nur ``[a-z0-9]`` behalten – Trenner, Leerraum und Sonderzeichen entfallen
    (``RE-2026/0042`` und ``re 2026 0042`` werden beide zu ``re20260042``)."""
    return _NOT_ALPHANUMERIC.sub('', _text(text).casefold())


def name_words(text) -> frozenset[str]:
    """Die Wörter eines Namens oder Belegtextes zum Vergleichen: ``casefold``, Buchstaben und Ziffern, mindestens
    ``MIN_WORD_LENGTH`` Zeichen, ohne reine Zahlen und ohne ``STOP_WORDS``."""
    return frozenset(word for word in _WORD.findall(_text(text).casefold())
                     if len(word) >= MIN_WORD_LENGTH and not word.isnumeric() and word not in STOP_WORDS)


def usable_gross(reading) -> int | None:
    """Der Bruttobetrag einer Lesung (``Database.get_document_reading``) in Minor Units, wenn sich nach ihm suchen lässt – sonst ``None``:
    Status ``ok``, Währung leer oder ``EUR``, Betrag eine ganze Zahl ungleich 0 (das Vorzeichen bleibt, gesucht wird ohne)."""
    if not isinstance(reading, dict) or reading.get('Status') != 'ok':
        return None
    currency = _text(reading.get('Currency')).strip().upper()
    if currency and currency != 'EUR':
        return None
    gross = reading.get('GrossMinor')
    return gross if is_int(gross) and gross != 0 else None


def direction_fits(kind, amount) -> bool:
    """Passt die Zahlungsrichtung einer Buchung zur Belegart? ``kind`` ist ``Kind`` der Lesung: Eine Rechnung (``invoice``, auch leer)
    wird bezahlt, erwartet wird ein Ausgang (``amount`` < 0); eine Gutschrift (``credit_note``) bringt Geld herein (``amount`` > 0).
    Ein Betrag, der keine ganze Zahl ist, und eine unbekannte Art „passen“: Es wird nichts geraten."""
    if not is_int(amount):
        return True
    if kind is None or kind == '' or kind == 'invoice':
        return amount < 0
    if kind == 'credit_note':
        return amount > 0
    return True


def usable_number(number: str) -> bool:
    """Taugt diese normalisierte Rechnungsnummer als Beweis? Mit Ziffern und Buchstaben ab ``MIN_NUMBER_LENGTH`` Zeichen, rein
    numerisch ab ``MIN_NUMERIC_NUMBER_LENGTH``; ohne Ziffer nie (ein Wort ist keine Nummer). Nach ``normalize_number`` stehen
    nur ``[a-z0-9]`` darin, ``isdigit`` meint also genau die ASCII-Ziffern. Öffentlich, weil auch ``payment_matching`` sie nutzt."""
    if number.isdigit():
        return len(number) >= MIN_NUMERIC_NUMBER_LENGTH
    return len(number) >= MIN_NUMBER_LENGTH and any(char.isdigit() for char in number)


def _day_reason(distance: int) -> str:
    if distance == 0:
        return REASON_SAME_DAY
    if distance > 0:
        return REASON_DAY_AFTER if distance == 1 else REASON_DAYS_AFTER.format(days=distance)
    return REASON_DAY_BEFORE if distance == -1 else REASON_DAYS_BEFORE.format(days=-distance)


def text_field(candidate: dict, key: str) -> str:
    """Der Wert von ``key`` als Text, auf ``MAX_FIELD_LENGTH`` Zeichen gekappt (``None`` und ein fehlender Schlüssel ergeben ``''``).
    Öffentlich, weil auch ``payment_matching`` sie nutzt."""
    return _text(candidate.get(key))[:MAX_FIELD_LENGTH]


def _rate(candidate, target: int, day: datetime.date | None, number: str, document_words: frozenset[str],
          kind) -> Suggestion | None:
    if not isinstance(candidate, dict):
        return None
    booking_id, amount = candidate.get('id'), candidate.get('amount')
    if not is_int(booking_id) or not is_int(amount) or abs(amount) != target:
        return None
    score = 0
    reasons = [REASON_AMOUNT]
    if number and any(number in normalize_number(text_field(candidate, key)) for key in ('text', 'number', 'recipient')):
        score += POINTS_NUMBER
        reasons.append(REASON_NUMBER)
    if document_words:
        common = len(name_words(text_field(candidate, 'recipient') + ' ' + text_field(candidate, 'contact_name')) & document_words)
        if common:
            score += POINTS_NAME_SEVERAL if common > 1 else POINTS_NAME_ONE
            reasons.append(REASON_NAME)
    distance = None
    booked = iso_day(candidate.get('date'))
    if day is not None and booked is not None:
        distance = (booked - day).days
        if -WINDOW_DAYS_BEFORE <= distance <= WINDOW_DAYS_AFTER:
            score += max(0, POINTS_DATE - abs(distance) // DATE_STEP_DAYS)
            reasons.append(_day_reason(distance))
    mismatch = not direction_fits(kind, amount)
    if mismatch:
        reasons.append(REASON_DIRECTION)
    linked = bool(candidate.get('linked'))
    if linked:
        reasons.append(REASON_LINKED)
    return Suggestion(booking_id, score, tuple(reasons), linked, distance, mismatch)


def _order(suggestion: Suggestion) -> tuple:
    distance = suggestion.day_distance
    return (suggestion.linked, suggestion.mismatch, -suggestion.score, _NO_DISTANCE if distance is None else abs(distance),
            suggestion.booking_id)


def rank_candidates(reading: dict, candidates: list[dict], doc_date: str | None) -> list[Suggestion]:
    """Die Kandidaten in eine Rangfolge bringen: höchstens ``MAX_SUGGESTIONS`` Vorschläge, nach ``(schon verknüpft, Richtung passt
    nicht, −Punkte, Abstand zum Belegdatum, ID)`` – die verknüpften zuletzt, innerhalb davon die mit unpassender Zahlungsrichtung
    (``direction_fits``, nach ``Kind`` der Lesung) hinter denen mit passender.

    ``reading``: die Lesung des Belegs (``Database.get_document_reading``). Ohne brauchbaren Bruttobetrag (``usable_gross``: Status
    ``ok``, Währung leer oder ``EUR``, Betrag ungleich 0) gibt es keine Vorschläge. ``doc_date``: das maßgebliche Datum des Belegs
    (der Aufrufer nimmt das gelesene, sonst das des Belegs). Jeder Kandidat ist ein ``dict`` mit ``id``, ``date``, ``amount``
    (Minor Units), ``text``, ``recipient``, ``number`` (Beleg-Nr.), ``contact_name`` und ``linked`` (trägt schon einen Beleg der Art
    ``receipt``); nur einer, dessen Betrag ohne Vorzeichen der des Belegs ist, kommt in Frage (die Abfrage filtert das schon, die
    Funktion verlässt sich nicht darauf) – das Vorzeichen schließt nie aus, es bestimmt nur die Richtung. Das Fenster der Abfrage
    (``candidate_window``) prüft sie nicht: Außerhalb davon gibt es nur keine Punkte für das Datum.

    Ein schiefer Wert (``None``, eine Zahl statt Text, ein kaputtes Datum) wirft nie: Das Signal fällt weg, ein Kandidat ohne ganze
    Zahlen als ``id`` und ``amount`` entfällt."""
    gross = usable_gross(reading)
    if gross is None:
        return []
    number = normalize_number(reading.get('InvoiceNumber'))
    if not usable_number(number):
        number = ''                                   # zu kurz oder ohne Ziffer: kein Beweis
    document_words = name_words(_text(reading.get('SenderName')) + ' ' + _text(reading.get('Text')))
    day = iso_day(doc_date)
    kind = reading.get('Kind')
    rated = (_rate(candidate, abs(gross), day, number, document_words, kind) for candidate in candidates)
    return heapq.nsmallest(MAX_SUGGESTIONS, (suggestion for suggestion in rated if suggestion is not None), key=_order)
