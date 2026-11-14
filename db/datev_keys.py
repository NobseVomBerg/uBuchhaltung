# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""BU-Schlüssel des DATEV-Buchungsstapels – reine Funktionen, keine Datenbank.

Der Export gibt den Schlüssel aus, den die Quelle einer Buchung mitgebracht hat
(``Bookings.DatevKey``, z. B. aus der WISO-Datenbank). Wo keiner gespeichert ist, leitet er ihn nach den
Regeln dieses Moduls ab. Sie sind dem Stapel nachgebildet, den WISO Mein Büro schreibt, gemessen an einem
Datenbestand – keine Vorgabe von DATEV und keine Zusicherung, dass die Kanzlei ihn so annimmt.

Der Schlüssel folgt der **Art des Zweckkontos**, nicht der Richtung der Geldbewegung: Eine Erstattung auf ein
Aufwandskonto bleibt Vorsteuer, eine Erstattung auf ein Erlöskonto Umsatzsteuer. Die Schlüssel heißen bei
WISO „Regelsatz“, „ermäßigt“ und „anderer Regelsatz/ermäßigter Satz“ – nicht „19 %“ und „7 %“: Im zweiten
Halbjahr 2020 (befristete Sätze 16 % und 5 %) tauschen die Zuordnungen.
"""
import datetime
import math

from .matching import (FORM_LIQUID_FIRST, FORM_TRANSFER, MIRROR_FORMS, ASSET_ACCOUNT_RANGE,
                       REVENUE_ACCOUNT_RANGE)

#: Art einer Buchung für den Schlüssel
KIND_NONE = 'none'                    # kein Schlüssel (Spiegel, Debitor als Zweckkonto, Umbuchung ohne Erlös-/Aufwands-/Anlagekonto)
KIND_INPUT = 'input'                  # Vorsteuer
KIND_VAT = 'vat'                      # Umsatzsteuer
KIND_INVOICE_CHAIN = 'invoice_chain'  # Rechnungskette: kein Schlüssel, aber eine Umsatzsteuer-Zeile (Spalte 40)

#: Die befristeten Sätze (Senkung der Umsatzsteuer): 2020-07-01 bis 2020-12-31, einschließlich
SWAP_FROM = datetime.date(2020, 7, 1)
SWAP_TO = datetime.date(2020, 12, 31)

#: Prozent: (Regelsatz, ermäßigt, anderer Regelsatz, anderer ermäßigter Satz) außerhalb und innerhalb der
#: befristeten Sätze – dieselben vier Schlüsselplätze, andere Sätze dahinter
_RATES = (19.0, 7.0, 16.0, 5.0)
_RATES_SWAP = (16.0, 5.0, 19.0, 7.0)

#: Die Schlüssel je Art, in der Reihenfolge von ``_RATES``
_KEYS = {
    KIND_INPUT: (401, 402, 7, 6),     # Vorsteuer
    KIND_VAT: (101, 102, 5, 4),       # Umsatzsteuer
}

#: Umsatzsteuer-Schlüssel mit Satz: Eine Zeile mit einem davon trägt in Spalte 40 den Satz
VAT_KEYS = frozenset({101, 102, 5, 4, 121, 122})

#: „Verwendung von Gegenständen“ (unentgeltliche Wertabgabe): 121 Regelsatz, 122 ermäßigt – mit denselben
#: befristeten Sätzen
_USE_KEYS = {121: 0, 122: 1}

MAX_KEY = 9999
BELEG_MAX = 36                        # Länge des Feldes „Belegfeld 1“

DEBTOR_RANGE = (10000, 69999)         # Personenkonten
EXPENSE_RANGE = (5000, 7999)          # Aufwand (SKR04, Klasse 5–7)

CONSULTANT_NO_RANGE = (1001, 9999999)  # ganze Zahl, nur ASCII-Ziffern ohne führende Null
CLIENT_NO_RANGE = (1, 99999)
CONSULTANT_NO_ERROR = 'Die DATEV-Beraternummer muss eine Zahl von 1001 bis 9999999 sein (nur Ziffern, ohne führende Null).'
CLIENT_NO_ERROR = 'Die DATEV-Mandantennummer muss eine Zahl von 1 bis 99999 sein (nur Ziffern, ohne führende Null).'


def percent(rate) -> float:
    """Der Steuersatz als auf zwei Stellen gerundete Prozentzahl – dieselbe Normierung wie
    ``InvoicesMixin._revenue_rate_key``: 0.19 und 19 sind beide 19.0. Kein Satz, 0, negative Werte (der
    Platzhalter −1 für § 19) und alles, was keine Zahl ist, sind 0.0."""
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value) or value <= 0:
        return 0.0
    return round(value * 100 if value <= 1 else value, 2)


def _date(value):
    """Ein Datum (``date``, ``datetime`` oder ISO-Text, auch mit Uhrzeit) – ``None``, wenn es unlesbar ist."""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    text = str(value)[:10]
    if len(text) != 10 or text[4] != '-' or text[7] != '-':      # nur JJJJ-MM-TT, nicht JJJJMMTT oder Wochendaten
        return None
    try:
        return datetime.date.fromisoformat(text)
    except ValueError:
        return None


def in_swap(date) -> bool:
    """Liegt das Datum in den befristeten Sätzen (2020-07-01 bis 2020-12-31)? Ein unlesbares Datum nicht."""
    day = _date(date)
    return day is not None and SWAP_FROM <= day <= SWAP_TO


def derive_key(kind, rate, date) -> int:
    """Der BU-Schlüssel zu Art, Steuersatz und Buchungsdatum – ``0``, wenn es keinen gibt: keine Art mit
    Schlüssel (``KIND_NONE``, ``KIND_INVOICE_CHAIN``), kein Satz, 0 % oder ein Satz außerhalb der vier. Der Satz
    steht wie in den Büchern (0.19 oder 19); ein unlesbares Datum gilt als außerhalb der befristeten Sätze."""
    keys = _KEYS.get(kind)
    if keys is None:
        return 0
    pct = percent(rate)
    if pct <= 0:
        return 0
    rates = _RATES_SWAP if in_swap(date) else _RATES
    return keys[rates.index(pct)] if pct in rates else 0


#: Schlüssel → Platz in ``_RATES`` (0 Regelsatz, 1 ermäßigt, 2 anderer Regelsatz, 3 anderer ermäßigter Satz)
_PLACE_OF_KEY = {**{key: place for keys in _KEYS.values() for place, key in enumerate(keys)}, **_USE_KEYS}


def key_rate(key, date):
    """Die Umkehrung von ``derive_key`` für den Textimport: der Steuersatz (als Bruch, 0.19) zu einem BU-Schlüssel
    und Datum – für die Schlüssel der Tabelle (401, 402, 7, 6, 101, 102, 5, 4) und für 121 (Regelsatz) und 122
    (ermäßigt) der Verwendung von Gegenständen. ``None``, wenn die Tabelle den Schlüssel nicht kennt: Dann gilt
    wie bisher ``TaxKeys``."""
    place = _PLACE_OF_KEY.get(parse_key(key))
    if place is None:
        return None
    return (_RATES_SWAP if in_swap(date) else _RATES)[place] / 100


def parse_key(value):
    """Ein BU-Schlüssel, wie ihn eine Quelle oder die Datenbank trägt, als ``int`` von 1 bis 9999 – alles andere
    ist ``None``: leer, 0, Text, der keine Ziffernfolge ohne führende Nullen ist, Wahrheitswerte, Brüche, zu große
    und negative Zahlen. So gelangt nie ein Wert in den Stapel, den nur ein Eingriff von Hand (SQL-Konsole) dort
    hingeschrieben haben kann."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        number = int(value)
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > len(str(MAX_KEY)) or not (text.isascii() and text.isdigit()) or str(int(text)) != text:
            return None
        number = int(text)
    else:
        return None
    return number if 1 <= number <= MAX_KEY else None


def checked_key(value):
    """Ein BU-Schlüssel zum Schreiben: ``None``, leer und 0 heißen „keiner“ (``None``), ein gültiger Schlüssel
    (``parse_key``) wird ``int``, alles andere ist ein ``ValueError``."""
    if value is None or (isinstance(value, str) and value.strip() in ('', '0')):
        return None
    if not isinstance(value, bool) and isinstance(value, (int, float)) and value == 0:
        return None
    key = parse_key(value)
    if key is None:
        raise ValueError(f'Ungültiger BU-Schlüssel: {value!r} (erlaubt ist eine ganze Zahl von 1 bis {MAX_KEY})')
    return key


def account_number(value):
    """Eine Kontonummer als ``int`` – Zahl oder Text aus Ziffern; sonst (leer, fehlt, ``4405a``) ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip() if value is not None else ''
    return int(text) if text.isascii() and text.isdigit() else None


def _within(number, bounds) -> bool:
    return number is not None and bounds[0] <= number <= bounds[1]


def _is_debtor(number) -> bool:
    return _within(number, DEBTOR_RANGE)


def _is_revenue(number) -> bool:
    return _within(number, REVENUE_ACCOUNT_RANGE)


def _is_expense_or_asset(number) -> bool:
    return _within(number, EXPENSE_RANGE) or _within(number, ASSET_ACCOUNT_RANGE)


def booking_kind(form, coa_number, counter_number, is_inflow, waiting_numbers=()) -> str:
    """Die Art einer Buchung, die keinen gespeicherten Schlüssel trägt – nach der Art ihres Zweckkontos.

    ``form`` ist die gelesene Form des Satzes (``db.matching``; ``None``, wenn sie unbekannt ist: dann zählt die
    Richtung der Geldbewegung, ``is_inflow``), ``coa_number``/``counter_number`` sind die Kontonummern der beiden
    Seiten, ``waiting_numbers`` die Wartekonten der offenen Posten (``Database.get_waiting_account_numbers``).

    * Spiegel (zwei Geldkonten): kein Schlüssel.
    * Umbuchung: Steht ein Debitor oder ein Wartekonto auf einer Seite, ist es die Rechnungskette (Forderung,
      „Wartekonto an Erlöskonto“, Gegenbuchung einer Stornorechnung): Schlüssel 0, aber eine Umsatzsteuer-Zeile.
      Sonst: ein Erlöskonto (4000–4999) auf einer Seite – Umsatzsteuer, ein Aufwands- oder Anlagekonto
      (5000–7999, 1–999) – Vorsteuer, alles andere – keiner. Auch die Umbuchung „Aufwand an Anlagekonto“ (die AfA)
      ist von der Art Vorsteuer; einen Schlüssel trägt sie nur deshalb nicht, weil sie keinen Steuersatz hat
      (``derive_key`` gibt ohne Satz 0).
    * Zahlung (das Zweckkonto steht vorn, bei ``liquid_first`` hinten): Debitor – kein Schlüssel; Erlöskonto –
      Umsatzsteuer, auch bei einer Erstattung (negativer Betrag); jedes andere Konto – Vorsteuer, auch bei einer
      Erstattung. Fehlt das Zweckkonto, entscheidet die Geldrichtung: Einnahme Umsatzsteuer, Ausgabe Vorsteuer."""
    by_money = KIND_VAT if is_inflow else KIND_INPUT
    if form is None:
        return by_money
    if form in MIRROR_FORMS:
        return KIND_NONE
    coa_number, counter_number = account_number(coa_number), account_number(counter_number)
    if form == FORM_TRANSFER:
        waiting = {account_number(number) for number in waiting_numbers}
        sides = (coa_number, counter_number)
        if any(_is_debtor(number) or number in waiting for number in sides if number is not None):
            return KIND_INVOICE_CHAIN
        if any(_is_revenue(number) for number in sides):
            return KIND_VAT
        if any(_is_expense_or_asset(number) for number in sides):
            return KIND_INPUT
        return KIND_NONE
    purpose = counter_number if form == FORM_LIQUID_FIRST else coa_number
    if purpose is None:
        return by_money
    if _is_debtor(purpose):
        return KIND_NONE
    return KIND_VAT if _is_revenue(purpose) else KIND_INPUT


def is_receivable_row(form, coa_number, counter_number, waiting_numbers=()) -> bool:
    """Ist das die Forderung einer Rechnung (oder die Gegenbuchung ihrer Stornorechnung)? Eine Umbuchung, bei der
    eine Seite ein Debitor und die andere ein Wartekonto ist – nur sie trägt in Spalte 114 das Leistungsdatum."""
    if form != FORM_TRANSFER:
        return False
    waiting = {account_number(number) for number in waiting_numbers}
    numbers = (account_number(coa_number), account_number(counter_number))
    return (any(_is_debtor(number) for number in numbers)
            and any(number is not None and number in waiting for number in numbers))


def is_vat_row(key, kind, rate) -> bool:
    """Trägt die Zeile in Spalte 40 („EU-Steuersatz“) den Satz? Ja bei einem Satz über 0 und einer
    Umsatzsteuer-Zeile: Der Schlüssel ist ein Umsatzsteuer-Schlüssel (``VAT_KEYS``), oder es gibt keinen (0) und die
    Art ist Umsatzsteuer oder Rechnungskette. Bei einem gespeicherten Schlüssel zählt allein dieser."""
    if percent(rate) <= 0:
        return False
    return key in VAT_KEYS or (key == 0 and kind in (KIND_VAT, KIND_INVOICE_CHAIN))


def beleg_field(text) -> str:
    """Belegfeld 1: die Beleg-Nr. ohne Leerzeichen und anderen Leerraum, höchstens 36 Zeichen (die Länge des
    DATEV-Feldes); sonst unverändert – WISO kürzt sie nicht auf 12 Stellen."""
    return ''.join(str(text or '').split())[:BELEG_MAX]


def _number(value, lowest, highest):
    """Eine Nummer als Text: nur ASCII-Ziffern ohne führende Null, ein Wert von ``lowest`` bis ``highest`` – sonst
    ``None``. Eine ganze Zahl (die Spalte einer Quelle) gilt als ihre Ziffern; Leerraum an den Rändern zählt nicht
    mit (die Aufrufer schneiden ihn ab)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        value = str(value) if value >= 0 else ''
    if not isinstance(value, str):
        return None
    if not (value.isascii() and value.isdigit()) or value[0] == '0' or len(value) > len(str(highest)):
        return None
    return value if lowest <= int(value) <= highest else None


def consultant_no(value):
    """Die Beraternummer (eine Zahl von 1001 bis 9999999, ohne führende Null) als Text – sonst ``None``."""
    return _number(value, *CONSULTANT_NO_RANGE)


def client_no(value):
    """Die Mandantennummer (eine Zahl von 1 bis 99999, ohne führende Null) als Text – sonst ``None``."""
    return _number(value, *CLIENT_NO_RANGE)
