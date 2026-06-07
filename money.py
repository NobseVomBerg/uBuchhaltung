"""Zentrale Geld-Arithmetik fuer PyBuch (Festkomma, exakt).

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
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

SCALE = 4
FACTOR = 10 ** SCALE          # 10000
_Q = Decimal(1).scaleb(-SCALE)  # Decimal('0.0001')
_ONE = Decimal(1)
_HUNDRED = Decimal(100)


# ── Parsing (Euro-Welt -> Decimal) ────────────────────────────────────────────

def _to_decimal(value):
    """Robuste Umwandlung in Decimal.

    Akzeptiert Decimal, int, float und str. Strings duerfen deutsches
    (1.234,56) oder englisches (1234.56) Format haben. float wird ueber str
    gewandelt, um Binaer-Artefakte zu vermeiden.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool ist Subtyp von int -> hier bewusst ablehnen
        raise TypeError("bool ist kein gueltiger Geldwert")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
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
            return Decimal(s)
        except InvalidOperation:
            raise ValueError(f"ungueltiger Geldwert: {value!r}")
    raise TypeError(f"nicht unterstuetzter Typ fuer Geldwert: {type(value).__name__}")


# ── Grenze Euro <-> Minor Units ───────────────────────────────────────────────

def to_minor(value) -> int:
    """Euro-Wert (Decimal/int/float/str) -> Minor Units (int), HALF_UP gerundet."""
    d = _to_decimal(value).quantize(_Q, rounding=ROUND_HALF_UP)
    return int(d * FACTOR)


def from_minor(minor: int) -> Decimal:
    """Minor Units (int) -> Euro-Decimal mit SCALE Nachkommastellen (exakt)."""
    return (Decimal(int(minor)) / FACTOR).quantize(_Q)


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
