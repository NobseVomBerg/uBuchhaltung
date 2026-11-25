# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Zentrale Geld-Arithmetik fuer uBuchhaltung (Festkomma, exakt).

Geldbetraege werden als ganzzahlige *Minor Units* gefuehrt: der Eurobetrag
multipliziert mit 10^SCALE. Bei SCALE=4 entspricht 1 Minor Unit = 0,0001 EUR
(ein Zehntausendstel). Damit sind Summen exakt (Integer-Addition) und auch
vierstellige Stueckpreise verlustfrei abbildbar.

Dieses Modul ist die EINZIGE Stelle, an der zwischen Anzeige-/Eingabe-Werten
(Euro als Decimal/str/float) und der internen Integer-Darstellung umgerechnet
wird. Alle Rundungen erfolgen kaufmaennisch (ROUND_HALF_UP).

Konvention:
- to_minor / from_minor: Grenze zwischen Euro-Welt und Minor-Units.
- Rechnen (tax_*, multiply, add/sub) findet in Minor Units statt.
- format_*: Ausgabe (Anzeige, Export). Standard-Anzeige mit 2 Nachkommastellen.
"""
import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

SCALE = 4
FACTOR = 10 ** SCALE          # 10000
_Q = Decimal(1).scaleb(-SCALE)  # Decimal('0.0001')
_CENT = Decimal('0.01')
_ONE = Decimal(1)
_HUNDRED = Decimal(100)

# Betrag aus einer Datei: deutsch (Tausenderpunkte nur in Dreiergruppen, danach ein Komma mit Nachkommateil)
# oder ohne Komma mit Punkt als Dezimalpunkt. ``[0-9]`` statt ``\d``: ``\d`` passt auch auf Ziffern anderer Schriften.
_AMOUNT_DE = re.compile(r'[+-]?(?:[0-9]{1,3}(?:\.[0-9]{3})+|[0-9]+),[0-9]+')
_AMOUNT_PLAIN = re.compile(r'[+-]?[0-9]+(?:\.[0-9]+)?')
# Betrag aus einer Datei, in der der Punkt IMMER ein Tausendertrenner ist (DKB-Umsatzliste): ganze Euro mit Tausenderpunkten in
# Dreiergruppen oder ohne Punkt, danach höchstens zwei Nachkommastellen nach einem Komma.
# Die erste Gruppe vor einem Tausenderpunkt beginnt nie mit 0 (``0.500`` ist kein Betrag)
_AMOUNT_GROUPED = re.compile(r'[+-]?(?:[1-9][0-9]{0,2}(?:\.[0-9]{3})+|[0-9]+)(?:,[0-9]{1,2})?')
_AMOUNT_MAX_LENGTH = 30
MAX_PARSED_AMOUNT = Decimal(10) ** 12      # ab hier ist ein Betrag aus einer Datei ein Fehler (wie importers.camt.MAX_AMOUNT)


# ── Parsing (Euro-Welt -> Decimal) ────────────────────────────────────────────

def _finite(value, original):
    """``value`` (Decimal), wenn es eine endliche Zahl ist; NaN und Infinity sind kein Geldwert."""
    if not value.is_finite():
        raise ValueError(f"ungueltiger Geldwert: {original!r}")
    return value


def _to_decimal(value):
    """Robuste Umwandlung in Decimal.

    Akzeptiert Decimal, int, float und str. Strings duerfen deutsches
    (1.234,56) oder englisches (1234.56) Format haben. float wird ueber str
    gewandelt, um Binaer-Artefakte zu vermeiden. NaN und Infinity (als Decimal,
    float oder Text) sind kein Geldwert: ``ValueError``.
    """
    if isinstance(value, Decimal):
        return _finite(value, value)
    if isinstance(value, bool):
        # bool ist Subtyp von int -> hier bewusst ablehnen
        raise TypeError("bool ist kein gueltiger Geldwert")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return _finite(Decimal(str(value)), value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValueError("leerer Geldwert")
        if ',' in s and '.' in s:
            # deutsches Format: '.' = Tausender, ',' = Dezimal
            s = s.replace('.', '').replace(',', '.')
        elif ',' in s:
            s = s.replace(',', '.')
        try:
            return _finite(Decimal(s), value)
        except InvalidOperation:
            raise ValueError(f"ungueltiger Geldwert: {value!r}")
    raise TypeError(f"nicht unterstuetzter Typ fuer Geldwert: {type(value).__name__}")


def parse_amount(text) -> Decimal:
    """Betrag aus einer Datei (Textexport, Kontoauszug) streng als Decimal lesen.

    Erlaubt sind nur ASCII-Ziffern mit einem Vorzeichen (``+``/``-``) vorn, und zwar entweder deutsch
    (``1.234,56``, ``-41,25``, ``3,5``: Tausenderpunkte nur in Dreiergruppen, danach Komma und mindestens
    eine Ziffer) oder ohne Komma (``1234.56``, ``12``: der Punkt ist der Dezimalpunkt, ``1.234`` ist also
    1,234). Leerraum außen wird entfernt, innen ist er ein Fehler; höchstens 30 Zeichen und betragsmäßig unter
    einer Billion (``MAX_PARSED_AMOUNT``). Alles andere –
    ``nan``, ``inf``, ``1e5``, ``1_000``, Ziffern anderer Schriften – ist ein ``ValueError``: ``float()``
    nähme es an.

    Bewusst anders als ``parse_grouped_amount`` (DKB-Umsatzliste): Hier ist ein Punkt ohne Komma ein Dezimalpunkt
    (``1.500`` ist 1,5), dort immer ein Tausendertrenner (``1.500`` ist 1500).
    """
    if not isinstance(text, str):
        raise ValueError(f"ungueltiger Betrag: kein Text ({type(text).__name__})")
    s = text.strip()
    if len(s) > _AMOUNT_MAX_LENGTH:
        raise ValueError("ungueltiger Betrag: zu lang")
    if _AMOUNT_DE.fullmatch(s):
        value = Decimal(s.replace('.', '').replace(',', '.'))
    elif _AMOUNT_PLAIN.fullmatch(s):
        value = Decimal(s)
    else:
        raise ValueError(f"ungueltiger Betrag: {s!r}")
    if abs(value) >= MAX_PARSED_AMOUNT:
        # Ein so großer Wert liefe später in to_minor oder beim Speichern (64 Bit) in einen technischen Fehler
        raise ValueError("ungueltiger Betrag: zu gross")
    return value


def parse_grouped_amount(text):
    """Betrag aus einer Datei, in der der Punkt IMMER ein Tausendertrenner ist (DKB-Umsatzliste), streng als ``Decimal`` –
    oder ``None``, wenn der Text keiner ist (nichts wird geraten, es gibt keinen Fehler).

    Die DKB lässt bei runden Beträgen die Nachkommastellen weg: ``-15.000`` heißt minus fünfzehntausend Euro. Gelesen
    wird deshalb nur deutsch: ohne Leerraum am Rand (auch U+00A0 und U+202F) und ohne ein angehängtes ``€`` oder ``EUR``
    (mit oder ohne Leerraum davor) ein Vorzeichen vorn, ganze Euro ohne Punkt oder mit Tausenderpunkten in Dreiergruppen, danach höchstens zwei
    Nachkommastellen nach einem Komma – nur ASCII-Ziffern, höchstens 30 Zeichen, betragsmäßig unter einer Billion
    (``MAX_PARSED_AMOUNT``). ``1.500`` ist 1500, ``-15.000`` ist -15000, ``1.234,56`` ist 1234,56, ``-15`` ist -15;
    ``12.5``, ``0.500``, ``1,234.56``, ``1e3``, ``nan``, Leerraum zwischen den Ziffern (``12 34`` ist nicht 1234), Ziffern
    anderer Schriften und ein leerer Text sind ``None``. Kein ``float()``.

    Bewusst anders als ``parse_amount`` (WISO-Textexport, PDF-Auszüge): Dort ist ein Punkt ohne Komma ein Dezimalpunkt
    (``1.500`` ist 1,5).
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    for suffix in ('€', 'EUR'):
        if s.endswith(suffix):
            s = s[:-len(suffix)].rstrip()
            break
    if len(s) > _AMOUNT_MAX_LENGTH or not _AMOUNT_GROUPED.fullmatch(s):
        return None
    value = Decimal(s.replace('.', '').replace(',', '.'))
    return None if abs(value) >= MAX_PARSED_AMOUNT else value


# ── Grenze Euro <-> Minor Units ───────────────────────────────────────────────

def to_minor(value) -> int:
    """Euro-Wert (Decimal/int/float/str) -> Minor Units (int), HALF_UP gerundet."""
    d = _to_decimal(value).quantize(_Q, rounding=ROUND_HALF_UP)
    return int(d * FACTOR)


def from_minor(minor: int) -> Decimal:
    """Minor Units (int) -> Euro-Decimal mit SCALE Nachkommastellen (exakt)."""
    return (Decimal(int(minor)) / FACTOR).quantize(_Q)


def round_to_cent(value) -> Decimal:
    """Euro-Wert (Decimal/int/float/str) -> auf den Cent gerundeter Decimal, EINMAL und kaufmaennisch (HALF_UP: ab der Haelfte weg von null).

    Kein Umweg ueber Minor Units: ``to_minor`` rundet zuerst auf vier Stellen – 1,00495 wuerde so zu 1,0050 und dann zu 1,01, richtig ist 1,00.
    float wird ueber str gewandelt (1.005 ist 1,005, nicht 1,00499…); NaN, Infinity und bool sind kein Geldwert (wie bei ``to_minor``).
    """
    return _to_decimal(value).quantize(_CENT, rounding=ROUND_HALF_UP)


# ── Rechnen in Minor Units ────────────────────────────────────────────────────

def multiply(minor: int, factor) -> int:
    """Minor Units * Faktor (z.B. Menge) -> Minor Units, HALF_UP gerundet.

    Beispiel Positionssumme: multiply(price_minor, quantity).
    """
    return int((Decimal(int(minor)) * _to_decimal(factor)).quantize(_ONE, rounding=ROUND_HALF_UP))


def round_minor(minor: int, dp: int = 2) -> int:
    """Rundet einen Minor-Wert kaufmaennisch auf dp Nachkommastellen.

    Ergebnis bleibt in Minor Units. Beispiel (SCALE=4): round_minor(37035, 2)
    -> 37000 (3,7035 EUR auf Cent gerundet = 3,70 EUR). Wird fuer Rechnungs-
    summen genutzt, die auf Cent gefuehrt werden, obwohl Stueckpreise vier
    Nachkommastellen haben duerfen.
    """
    if dp >= SCALE:
        return int(minor)
    step = 10 ** (SCALE - dp)        # dp=2 -> 100 Minor Units = 1 Cent
    return int((Decimal(int(minor)) / step).quantize(_ONE, rounding=ROUND_HALF_UP)) * step


def prorate(total_minor: int, part_minor: int, whole_minor: int) -> int:
    """Anteil ``part/whole`` von ``total`` in Minor Units, HALF_UP gerundet.

    Beispiel: der Steueranteil einer Teilzahlung = prorate(Steuer, Teilzahlung, Brutto).
    Exakt in Decimal, kein float.
    """
    return int((Decimal(int(total_minor)) * Decimal(int(part_minor)) / Decimal(int(whole_minor)))
               .quantize(_ONE, rounding=ROUND_HALF_UP))


PAID_RESIDUE = 10 ** (SCALE - 2)   # 0,01 EUR: ein Rest darunter gilt als beglichen


def is_covered(total_minor: int, paid_minor: int) -> bool:
    """Deckt ``paid`` den Betrag ``total`` – bis auf einen Rest unter einem Cent?

    Vorzeichenbewusst: Eine Gutschrift (negativer Betrag) wird durch negative Zahlungen gedeckt.
    Eine Zahlung in Gegenrichtung oder ohne Betrag deckt nie, ein Betrag von 0 ebenso wenig.
    Die Überzahlung gilt als gedeckt.
    """
    sign = (total_minor > 0) - (total_minor < 0)
    return paid_minor * sign > 0 and (total_minor - paid_minor) * sign < PAID_RESIDUE


def tax_from_net(net_minor: int, rate) -> int:
    """Steuerbetrag aus Netto. rate als Prozentzahl (z.B. 19 oder 7)."""
    r = _to_decimal(rate)
    return int((Decimal(int(net_minor)) * r / _HUNDRED).quantize(_ONE, rounding=ROUND_HALF_UP))


def tax_from_gross(gross_minor: int, rate) -> int:
    """Im Brutto enthaltener Steueranteil. rate als Prozentzahl."""
    r = _to_decimal(rate)
    g = Decimal(int(gross_minor))
    net = g / (_ONE + r / _HUNDRED)
    return int((g - net).quantize(_ONE, rounding=ROUND_HALF_UP))


def gross_from_net(net_minor: int, rate) -> int:
    """Brutto = Netto + Steuer (auf Netto)."""
    return int(net_minor) + tax_from_net(net_minor, rate)


# ── Ausgabe / Formatierung ────────────────────────────────────────────────────

def _quantize_for_display(minor: int, dp: int) -> Decimal:
    q = Decimal(1).scaleb(-dp) if dp > 0 else _ONE
    return from_minor(minor).quantize(q, rounding=ROUND_HALF_UP)


def format_plain(minor: int, dp: int = 2) -> str:
    """Maschinenformat mit '.' als Dezimaltrenner, z.B. '1234.56' (fuer XML/CSV)."""
    d = _quantize_for_display(minor, dp)
    return f"{d:.{dp}f}"


def format_de(minor: int, dp: int = 2) -> str:
    """Deutsches Anzeigeformat mit Tausenderpunkt und Dezimalkomma: '1.234,56'."""
    d = _quantize_for_display(minor, dp)
    sign = '-' if d < 0 else ''
    d = abs(d)
    int_part, _, frac_part = f"{d:.{dp}f}".partition('.')
    groups = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    int_grouped = '.'.join(groups)
    if dp:
        return f"{sign}{int_grouped},{frac_part}"
    return f"{sign}{int_grouped}"


def fmt_eur(value, symbol=True, dp: int = 2) -> str:
    """Anzeige-Betrag im deutschen Format: ``36.153,58 €`` (symbol=False: ``36.153,58``).

    Nimmt Decimal/int/float/str (auch Minor-Units-Ergebnisse als Decimal);
    ``None``/leer -> ``'-'``. NUR für Anzeige-Text – Werte in ``<input value>``,
    ``data-``-Attributen, JSON und allem, was zurückgeschickt oder per JS
    weitergerechnet wird, bleiben im Punkt-Format (``format_plain``).
    """
    if value is None or value == '':
        return '-'
    text = format_de(to_minor(value), dp)
    return text + ' \u20ac' if symbol else text
