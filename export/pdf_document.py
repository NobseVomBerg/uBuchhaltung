# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Gemeinsame Bausteine für Geschäftsdokument-PDFs (Rechnung & Angebot).

Beide Dokumente teilen denselben Aufbau – Kopf (Logo/Titel/Meta), Absenderzeile,
Empfängeradresse, Positionstabelle mit Summen, Fußzeile – und unterscheiden sich nur
in Titel, Zusatzfeldern (Zahlung/Bank vs. Gültigkeit) und optionalem Fließtext.

Diese Helfer arbeiten auf einer ``ops``-Liste (PDF-Content-Operatoren innerhalb eines
BT/ET-Textobjekts) und positionieren **absolut** über die Text-Matrix (``Tm``). Damit
sind Spalten – anders als bei der früheren Leerzeichen-Ausrichtung in proportionaler
Schrift – sauber ausgerichtet; Geldbeträge stehen rechtsbündig.

Schriften (aus :mod:`export.pdf_core`):  /F1 Helvetica · /F2 Bold · /F3 Oblique.
Ein optionales Logo ist als /Logo zeichenbar.
"""
import re
import html as _htmlmod
from decimal import Decimal
from .pdf_core import escape_pdf_string, logo_display_size
from money import fmt_eur, format_de
from tax_groups import group_label

# A4 hochkant, Seitenränder
PAGE_W, PAGE_H = 595, 842
LEFT = 50
RIGHT = 545                       # rechter Rand (rechtsbündige Beträge)

# Positionstabellen-Spalten (X in pt).
# Erste vier Spalten linksbündig (Pos schmal, da max. 3-stellig), die beiden
# Geldspalten rechtsbündig an *_R – Header und Werte am selben rechten Rand.
COL_POS = 50                      # Pos.       linksbündig (schmal)
COL_QTY = 80                      # Menge      linksbündig
COL_UNIT = 120                    # Einheit    linksbündig
COL_DESC = 165                    # Bezeichnung linksbündig
COL_PRICE_R = 470                 # Einzelpreis rechtsbündig
COL_TOTAL_R = RIGHT               # Gesamt      rechtsbündig
# Nur bei einem Beleg mit Steuersatz je Position (``draw_item_table(…, tax_groups=…)``): die Spalte „USt“, rechtsbündig wie die Geldspalten,
# so weit rechts, wie die Bezeichnung sonst reicht (``COL_PRICE_R - _PRICE_ROOM``: so breit darf ein Einzelpreis werden). Die Bezeichnung endet
# ``_RATE_COL_W`` links davon – die Breite fasst den längsten Satz („99,99 %“, 33 pt in 9 pt) mit Abstand. Das ist die Lage für gewöhnliche
# Preise; braucht ein Einzelpreis des Belegs mehr Platz, rückt die Spalte nach links (``_rate_layout``)
_PRICE_ROOM = 65
COL_RATE_R = COL_PRICE_R - _PRICE_ROOM
_RATE_COL_W = 40
_RATE_PRICE_GAP = 3.2             # Abstand, den der Satz zum Einzelpreis einer Zeile mindestens behält: 3 pt, dazu die Rundung der Textlage auf 0,1 pt
_MIN_DESC_W = 120                 # so schmal macht ein breiter Einzelpreis die Bezeichnung höchstens

# Helvetica-Zeichenbreiten (AFM, Einheiten/1000 em). Echte Metriken statt
# Schätzung – nur damit liegen rechtsbündige Spalten exakt auf einer Kante.
_W_REG = {
    ' ': 278, '!': 278, '"': 355, '#': 556, '$': 556, '%': 889, '&': 667, "'": 191,
    '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333, '.': 278, '/': 278,
    '0': 556, '1': 556, '2': 556, '3': 556, '4': 556, '5': 556, '6': 556, '7': 556,
    '8': 556, '9': 556, ':': 278, ';': 278, '<': 584, '=': 584, '>': 584, '?': 556,
    '@': 1015, 'A': 667, 'B': 667, 'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778,
    'H': 722, 'I': 278, 'J': 500, 'K': 667, 'L': 556, 'M': 833, 'N': 722, 'O': 778,
    'P': 667, 'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944,
    'X': 667, 'Y': 667, 'Z': 611, '[': 278, '\\': 278, ']': 278, '^': 469, '_': 556,
    '`': 333, 'a': 556, 'b': 556, 'c': 500, 'd': 556, 'e': 556, 'f': 278, 'g': 556,
    'h': 556, 'i': 222, 'j': 222, 'k': 500, 'l': 222, 'm': 833, 'n': 556, 'o': 556,
    'p': 556, 'q': 556, 'r': 333, 's': 500, 't': 278, 'u': 556, 'v': 500, 'w': 722,
    'x': 500, 'y': 500, 'z': 500, '{': 334, '|': 260, '}': 334, '~': 584,
    'ä': 556, 'ö': 556, 'ü': 556, 'ß': 556, 'Ä': 667, 'Ö': 778, 'Ü': 722, '€': 556,
    '·': 278, '–': 556, '…': 1000,
}
_W_BOLD = {
    ' ': 278, '!': 333, '"': 474, '#': 556, '$': 556, '%': 889, '&': 722, "'": 238,
    '(': 333, ')': 333, '*': 389, '+': 584, ',': 278, '-': 333, '.': 278, '/': 278,
    '0': 556, '1': 556, '2': 556, '3': 556, '4': 556, '5': 556, '6': 556, '7': 556,
    '8': 556, '9': 556, ':': 333, ';': 333, '<': 584, '=': 584, '>': 584, '?': 611,
    '@': 975, 'A': 722, 'B': 722, 'C': 722, 'D': 722, 'E': 667, 'F': 611, 'G': 778,
    'H': 722, 'I': 278, 'J': 556, 'K': 722, 'L': 611, 'M': 833, 'N': 722, 'O': 778,
    'P': 667, 'Q': 778, 'R': 722, 'S': 667, 'T': 611, 'U': 722, 'V': 667, 'W': 944,
    'X': 667, 'Y': 667, 'Z': 611, '[': 333, '\\': 278, ']': 333, '^': 584, '_': 556,
    '`': 333, 'a': 556, 'b': 611, 'c': 556, 'd': 611, 'e': 556, 'f': 333, 'g': 611,
    'h': 611, 'i': 278, 'j': 278, 'k': 556, 'l': 278, 'm': 889, 'n': 611, 'o': 611,
    'p': 611, 'q': 611, 'r': 389, 's': 556, 't': 333, 'u': 611, 'v': 556, 'w': 778,
    'x': 556, 'y': 556, 'z': 500, '{': 389, '|': 280, '}': 389, '~': 584,
    'ä': 556, 'ö': 611, 'ü': 611, 'ß': 611, 'Ä': 722, 'Ö': 778, 'Ü': 722, '€': 556,
    '·': 278, '–': 556, '…': 1000,
}


def _esc(s):
    return escape_pdf_string(str(s if s is not None else ''))


def text_width(s, size, font='/F1'):
    """Exakte Textbreite in pt anhand der Helvetica-AFM-Metriken."""
    widths = _W_BOLD if font == '/F2' else _W_REG
    total = sum(widths.get(ch, 556) for ch in str(s))
    return total / 1000.0 * size


def text(ops, x, y, s, font='/F1', size=10):
    """Linksbündige Textzelle an absoluter Position."""
    ops.append(f"{font} {size} Tf")
    ops.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm ({_esc(s)}) Tj")


def text_right(ops, x_right, y, s, font='/F1', size=10):
    """Rechtsbündige Textzelle (endet exakt bei x_right)."""
    text(ops, x_right - text_width(s, size, font), y, s, font, size)


# ── Briefkopf (wie Eingabeformular: Logo links, Meta rechts, Titel darunter) ──

# Meta-Block rechts: Label-Spalte / Wert-Spalte. Die Label-Spalte fasst die längste Bezeichnung („Rechnungsdatum:“, 81 pt
# in 10 pt) mit Abstand zum Wert
META_LABEL_X = 378
META_VALUE_X = 470
LOGO_TOP = 800            # Oberkante Logo / Meta-Block
LOGO_BOX_W = 180          # Logo-Darstellung: max. Breite/Höhe in pt (Pixel >> pt
LOGO_BOX_H = 85           #   ⇒ scharf, nicht verpixelt)

# Empfängeradresse: gegenüber Absenderzeile etwas tiefer und leicht eingerückt
ADDR_INDENT = 12         # ~2 Zeichen nach rechts
ADDR_TOP = 673           # näher an die eigene Anschrift heran
SENDER_GRAY = "0.4 0.4 0.4"   # eigene Anschrift im Adressfeld dunkelgrau


def address_block(company, name, addr_extra, street, postal, city, country=None):
    """Empfänger-Adressblock zusammensetzen (Firmenname nicht doppeln).

    Reihenfolge: Firmenname/Name · (abweichender Personenname) · Zusatzzeile ·
    Straße · PLZ Ort · (Land, nur bei Ausland). Die Zusatzzeile steht – falls
    vorhanden – zwischen Firmenname und Straße.
    """
    first = company or name
    lines = [first]
    if name and name != first:
        lines.append(name)
    if addr_extra:
        lines.append(addr_extra)
    lines.append(street)
    lines.append(f"{postal} {city}".strip())
    # Land nur bei Auslandsadressen mitdrucken (Inland 'DE' weglassen)
    if country and str(country).strip().upper() not in ('', 'DE', 'DEUTSCHLAND', 'GERMANY'):
        lines.append(str(country).strip())
    return [ln for ln in lines if ln]


def draw_letterhead(ops, image, title, meta_rows, sender_line, address_lines, subtitle=None):
    """Briefkopf zeichnen und y unterhalb des Titels zurückgeben.

    Aufbau (wie das Eingabeformular):
      - Logo oben **links**
      - Meta-Block (Datum, Nr., Kunden-Nr.) oben **rechts**, Label/Wert-Spalten
      - kleine Absenderzeile (kursiv) + Empfängeradresse links
      - Titel (fett) rechtsbündig, beginnend an der Meta-Spalte
      - optional ``subtitle`` unter dem Titel (Stornorechnung: „zu Rechnung Nr. … vom …“): eine Zeile, die bis
        zum rechten Rand verkleinert wird – passt sie auch klein nicht, bricht sie an Wortgrenzen um
    """
    # Logo oben links (Bild-XObject; q/cm/Do/Q ist Grafik, hier vor dem Text).
    # Pixelmaße bleiben hochauflösend, die Darstellung wird in eine Punkt-Box
    # eingepasst ⇒ scharf statt verpixelt.
    if image:
        dw, dh = logo_display_size(image, LOGO_BOX_W, LOGO_BOX_H)
        # +10: Logo etwas höher als der Meta-Block (LOGO_TOP bleibt für Meta unverändert)
        ops.append(f"q {dw:.1f} 0 0 {dh:.1f} {LEFT} {LOGO_TOP - dh + 10:.1f} cm /Logo Do Q")

    # Meta-Block oben rechts
    my = LOGO_TOP - 8
    for label, value in meta_rows:
        text(ops, META_LABEL_X, my, label, size=10)
        text(ops, META_VALUE_X, my, str(value), size=10)
        my -= 14

    # Absenderzeile (klein, dunkelgrau) + Empfängeradresse links – beide um
    # ~2 Zeichen eingerückt (eigene Anschrift gleich ausgerichtet wie die
    # Empfängeradresse).
    if sender_line:
        ops.append(f"{SENDER_GRAY} rg")
        text(ops, LEFT + ADDR_INDENT, 700, sender_line, font='/F3', size=8)
        ops.append("0 0 0 rg")
    y = ADDR_TOP
    for line in address_lines:
        if line:
            text(ops, LEFT + ADDR_INDENT, y, line, font='/F1', size=11)
            y -= 15

    # Titel: gleiche X-Position wie der Meta-Block oben rechts, auf Höhe der
    # ersten Empfängeradress-Zeile; kleiner als zuvor, aber größer als die
    # Tabellen-Header.
    text(ops, META_LABEL_X, ADDR_TOP, title, font='/F2', size=13)
    if subtitle:
        max_w, size = RIGHT - META_LABEL_X, 9
        while size > 7 and text_width(subtitle, size) > max_w:
            size -= 0.5
        lines, current = [], ''
        for word in subtitle.split(' '):
            trial = f'{current} {word}'.strip()
            if current and text_width(trial, size) > max_w:
                lines.append(current)
                current = word
            else:
                current = trial
        lines.append(current)
        for number, line in enumerate(lines):
            text(ops, META_LABEL_X, ADDR_TOP - 15 - 11 * number, line, size=size)

    # Tabelle beginnt unterhalb von Empfängeradresse UND Meta-Block. Zusätzlicher
    # Abstand (~2 Header-Zeilen), damit die Header-Zeile nicht ins Anschriftfenster
    # von Briefumschlägen rutscht.
    return min(y, my) - 16 - 26


def draw_footer_columns(ops, line_ops, columns, y_top=66):
    """Dreispaltige Fußzeile am Seitenfuß (Anschrift | Kontakt | Bankverbindung).

    columns: Liste von 3 Zeilenlisten. Wird tief unten auf der Seite verankert
    (~3 Zeilen Höhe), sodass sich die Trennlinie nicht verschiebt. Nicht kursiv.
    """
    line_ops.append(f"{LEFT} {y_top + 8:.1f} m {RIGHT} {y_top + 8:.1f} l S")
    xs = [LEFT, 235, 410]
    for x, lines in zip(xs, columns):
        yy = y_top - 3            # etwas mehr Abstand unter der Trennlinie
        for ln in lines:
            if ln:
                text(ops, x, yy, ln, font='/F1', size=8)
            yy -= 11


# ── Positionstabelle ──────────────────────────────────────────────────────────

def _fmt_money(value):
    return fmt_eur(value, symbol=False)


def _fmt_rate(pct):
    """Ein Steuersatz (Prozentzahl) mit Komma und ohne überflüssige Nullen: ``19 %``, ``5,5 %``."""
    return format(Decimal(str(pct)).normalize(), 'f').replace('.', ',') + ' %'


_ROW_LEADING = 13                 # Zeilenabstand innerhalb einer Position


def _split_wide_word(word, max_w, size):
    """Ein Wort ohne Leerzeichen in Stücke zerlegen, von denen keines breiter ist als ``max_w`` (``text_width``: dieselbe Rechnung, in ganzen
    Einheiten der Schrift summiert). Jedes Zeichen wird genau einmal gemessen – die Bezeichnung eines Belegs ist ein Freitext ohne Längengrenze."""
    pieces, start, units = [], 0, 0
    for index, char in enumerate(word):
        width = _W_REG.get(char, 556)
        if index > start and (units + width) / 1000.0 * size > max_w:
            pieces.append(word[start:index])
            start, units = index, 0
        units += width
    pieces.append(word[start:])
    return pieces


def _wrap_description(desc, max_w, size, break_words=False):
    """Bezeichnung in Zeilen zerlegen: ``;`` → Zeilenumbruch, lange Zeilen am
    Wortrand umbrechen. Liefert immer mindestens eine (ggf. leere) Zeile.

    ``break_words``: Ein Wort, das allein breiter ist als ``max_w``, steht auf einer eigenen Zeile und bricht dort mitten im Wort um
    (``_split_wide_word``) – sonst läuft es über die Spalte hinaus. Nur der Druck mit Steuersatz je Position setzt es (dort stünde das Wort
    über der Spalte „USt“); der übrige Druck bricht, wie er immer brach."""
    lines = []
    for segment in str(desc or '').split(';'):
        seg = segment.strip()
        if not seg:
            continue
        cur = ''
        for word in seg.split(' '):
            if break_words and text_width(word, size) > max_w:
                if cur:
                    lines.append(cur)
                *whole, cur = _split_wide_word(word, max_w, size)
                lines.extend(whole)
                continue
            trial = (cur + ' ' + word).strip()
            if cur and text_width(trial, size) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines or ['']


_TOTALS_HEIGHT = 70               # geschätzte Höhe des Summenblocks (Seitenumbruch)
_TAX_ROW = 14                     # Zeilenabstand der Steuerzeilen im Summenblock; jede Satzgruppe über die erste macht ihn um eine höher
_NOTE_SIZE = 9                    # Hinweis unter dem Summenblock: Schriftgröße, Zeilenabstand und
_NOTE_LEADING = 12                #   Abstand der ersten Zeile unter der Schlusslinie
_NOTE_TOP = 14
# Zeilen, auf die der Hinweis höchstens kommt: Ein Befreiungsgrund hat höchstens ``tax_groups.MAX_REASON_LENGTH`` + 1 Zeichen (``einvoice_model._reason_text``), jedes
# höchstens 1,015 Geviert breit (das „@“; bei 9 pt gut 9 pt): zusammen höchstens 2 750 pt. Zwei aufeinanderfolgende Zeilen sind zusammen breiter als eine (495 pt) – sonst hätte
# das erste Wort der zweiten noch auf die erste gepasst –, also braucht der Text höchstens 1 + 2 × 2 750 / 492 = 12 Zeilen (gemessen an Wörtern aus 27 „@“: 10); ein längeres
# Wort bricht mitten im Wort und füllt die Zeilen. Zwölf Zeilen sind so die Grenze, die ein Grund nie überschreitet; jeder längere Text endet mit „…“ statt unter dem Seitenrand
_NOTE_MAX_LINES = 12


def _note_lines(note, max_w, size):
    """Der Hinweis unter dem Summenblock in Zeilen – gemessen, wie er gedruckt wird: nach den WinAnsi-Ersetzungen (ein Pfeil steht als „->“ da, nicht als ein Zeichen) und mit den
    Breiten der eingebetteten Schrift (``pdf_worktime.info_lines``, dieselben Bausteine wie die Info-Spalte des Stundenzettels). Die Helvetica-Tabelle ``_W_REG`` kennt weder
    „Œ“ noch „—“, „‰“ oder „™“ (sie zählt 556 statt 1000): Ein Grund mit solchen Zeichen liefe über den rechten Rand. Am Wortrand umbrochen, ein Wort, das allein breiter ist als
    die Zeile, mitten im Wort; Leerraum zählt als ein Leerzeichen, ein ``;`` trennt keine Zeile. Höchstens ``_NOTE_MAX_LINES``, die letzte endet dann mit „…“ – nichts fällt weg,
    ohne dass man es sieht. Die Laufzeit hängt nur von den gedruckten Zeilen ab."""
    # erst zur Laufzeit: ``pdf_worktime`` zieht ``server.pages_worktime`` und damit das Serverpaket herein (gut 0,4 s Importzeit) – das Rechnungs-PDF braucht es nur hierfür
    from .pdf_worktime import info_lines
    return info_lines(note, max_w, size, _NOTE_MAX_LINES)


def _table_header(flow, rate_right=None):
    """Tabellen-Kopfzeile an flow.y setzen (Unterstrich-Linie in flow.line_ops). ``rate_right``: der rechte Rand der Spalte „USt“ (``None``: ohne
    die Spalte)."""
    ops = flow.ops
    y = flow.y - 14
    text(ops, COL_POS, y, "Pos.", font='/F2', size=10)
    text(ops, COL_QTY, y, "Menge", font='/F2', size=10)
    text(ops, COL_UNIT, y, "Einheit", font='/F2', size=10)
    text(ops, COL_DESC, y, "Bezeichnung", font='/F2', size=10)
    if rate_right is not None:
        text_right(ops, rate_right, y, "USt", font='/F2', size=10)
    text_right(ops, COL_PRICE_R, y, "Einzelpreis", font='/F2', size=10)
    text_right(ops, COL_TOTAL_R, y, "Gesamt", font='/F2', size=10)
    y -= 4
    flow.line_ops.append(f"{LEFT} {y:.1f} m {RIGHT} {y:.1f} l S")
    flow.y = y - 13


def _price_text(it, currency):
    """Der Einzelpreis einer Position, wie die Tabelle ihn druckt."""
    return f"{_fmt_money(it.get('price') or 0)} {currency}"


def _rate_layout(items, currency):
    """Die Lage der Spalte „USt“ und die Breite der Bezeichnung eines Belegs mit Steuersatz je Position – einmal je Beleg bestimmt, auf jeder Seite
    dieselbe.

    Gemessen wird der breiteste Einzelpreis des Belegs, wie die Tabelle ihn druckt (Schrift und Größe der Zeilen). Passt er samt Abstand in die
    Preisspalte (``_PRICE_ROOM``), bleibt alles, wo es war: die Spalte bei ``COL_RATE_R``. Ist er breiter, rückt die Spalte um das Fehlende nach
    links, und die Bezeichnung wird um dasselbe schmaler – nie unter ``_MIN_DESC_W``: Reicht auch das nicht, bleibt es bei dieser Breite, und die
    Spalte berührt den Einzelpreis.

    Liefert ``(rechter Rand der Spalte, größte Breite der Bezeichnung, ob der Einzelpreis auch so nicht Platz hat)``."""
    widest = max((text_width(_price_text(it, currency), 9) for it in items), default=0.0)
    missing = max(0.0, widest + _RATE_PRICE_GAP - _PRICE_ROOM)
    desc_w = COL_RATE_R - _RATE_COL_W - COL_DESC
    shift = min(missing, desc_w - _MIN_DESC_W)
    return COL_RATE_R - shift, desc_w - shift, missing > shift


def draw_item_table(flow, items, *, tax_rate_pct, sum_net, tax_amount, sum_gross,
                    currency='EUR', show_tax=True, tax_note=None, tax_groups=None, exempt_rates=frozenset(), document_number=''):
    """Positionstabelle mit rechtsbündigen Geldspalten + Summenblock.

    Seitenfähig: passt eine Position nicht mehr auf die Seite, wird umgebrochen
    und die Kopfzeile auf der Folgeseite wiederholt. Der Summenblock wird nicht
    von den Positionen getrennt (eigener Umbruch, falls er nicht mehr passt).

    items: Liste von Dicts mit pos, quantity, unit, description, price, total.
    Bezeichnungen dürfen mehrzeilig sein (``;`` trennt Zeilen); die übrigen
    Spalten werden dann oben (top) ausgerichtet.

    tax_rate_pct: der Satz im Kopf für die Steuerzeile eines Belegs mit EINEM Satz – eine Zahl (``:g``, mit Punkt: ``5.5``) oder ein fertiger Text, den die Aufrufer
    für einen Beleg mit Momentaufnahme eines Steuersatzes übergeben (mit Komma: ``5,5``, ``einvoice_model.snapshot_rate_text``).

    tax_note: optionaler Hinweis unter der Schlusslinie des Summenblocks (Kleinunternehmer:
    § 19 UStG; ein Beleg mit einem steuerfreien Steuersatz der Stammdaten: dessen Befreiungsgrund), ab dem linken
    Seitenrand (``LEFT``) auf die Breite bis ``RIGHT`` umbrochen – der Standardsatz passt in eine Zeile, ein Grund
    mit bis zu 300 Zeichen braucht ein paar (``_note_lines``: gemessen, wie der Text gedruckt wird, mit den Breiten der eingebetteten
    Schrift; höchstens ``_NOTE_MAX_LINES``, dann endet er mit „…“). Der Text steht, wie er ist: Ein Semikolon trennt keine Zeile, ein Wort, das
    breiter ist als die Zeile, bricht mitten im Wort um – nichts läuft über den Rand, nichts fällt weg, ohne dass man es sieht. Er gehört zum Block:
    Passt er nicht mehr auf die Seite, geht er mit dem Summenblock auf die nächste.

    tax_groups: nur für einen Beleg mit Steuersatz je Position (``Invoices.MixedTaxRates``; die Aufrufer übergeben sie nur dann,
    auch wenn es dann nur eine Gruppe ist) – ``tax_groups.RateGroup``, Satz absteigend. ``None`` (jeder andere Beleg): der Druck mit
    EINEM Satz, ``tax_rate_pct`` und die einzelne Steuerzeile, Zeichen für Zeichen wie immer. Mit Gruppen steht in der Tabelle
    zwischen Bezeichnung und Einzelpreis die Spalte „USt“ (je Position ``it['rate']``, die Prozentzahl; die Bezeichnung bricht
    entsprechend früher um, ein Wort, das breiter ist als die Spalte, mitten im Wort), und der Summenblock nennt unter „Summe netto“
    je Gruppe eine Zeile „zzgl. 7 % MwSt. auf 50,00 EUR:“ mit der Steuer der Gruppe (Minor Units, wie sie sind – bei einer
    Stornorechnung negativ), dann den Gesamtbetrag; jede Gruppe über die erste macht den Block um eine Zeile höher (Seitenumbruch).
    Passt der Block auch auf eine eigene Seite nicht (ab 46 Gruppen), brechen die Gruppenzeilen auf die Folgeseite um: Auf einer Seite
    stehen so viele, wie passen, und der Gesamtbetrag nie allein. ``tax_rate_pct`` druckt dann nichts: Der Satz im Kopf ist bei so einem
    Beleg nur ein Platzhalter. Ohne Steuerausweis (``show_tax`` falsch; § 19 UStG – nie bei einem Beleg mit Steuersatz je Position)
    gelten die Gruppen nicht.

    exempt_rates: die Sätze (``RateGroup.pct``) der Gruppen, die steuerfrei sind (die Momentaufnahme eines steuerfreien Steuersatzes der
    Stammdaten, ``einvoice_model.PerItemRates.exempt``): Ihre Zeile heißt „steuerfrei auf 80,00 EUR:“ statt „zzgl. 0 % MwSt. auf 80,00 EUR:“
    (``tax_groups.group_label``); die Spalte „USt“ nennt für ihre Positionen weiter „0 %“. Ohne Gruppen gilt es nicht – ein Beleg mit EINEM Satz druckt
    auch als steuerfreier „zzgl. 0% MwSt.:“ wie jeder Beleg mit 0 %.

    Die Spalte „USt“ steht rechts der Bezeichnung dort, wo ein gewöhnlicher Einzelpreis noch Platz hat (``COL_RATE_R``); ein breiterer
    Einzelpreis des Belegs rückt sie nach links (``_rate_layout``), auf allen Seiten gleich. ``document_number``: die Belegnummer, bereits
    bereinigt, für die eine Zeile im Server-Log, wenn auch das nicht reicht.
    """
    per_item = tax_groups is not None and show_tax
    # Bezeichnung darf nicht in die Einzelpreis-Spalte laufen (mit Steuersatz je Position nicht in die Spalte „USt“).
    if per_item:
        rate_right, desc_max_w, too_wide = _rate_layout(items, currency)
        if too_wide:
            print(f"PDF {document_number}: Ein Einzelpreis ist so breit, dass die Bezeichnung nicht noch schmaler werden kann – "
                  f"die Spalte „USt“ berührt den Einzelpreis")
    else:
        rate_right = None
        desc_max_w = (COL_PRICE_R - _PRICE_ROOM) - COL_DESC
    _table_header(flow, rate_right)

    for it in items:
        desc_lines = _wrap_description(it.get('description'), desc_max_w, 9, break_words=per_item)
        row_h = _ROW_LEADING * len(desc_lines)
        if flow.y - row_h < flow.bottom:
            flow.new_page()
            _table_header(flow, rate_right)
        ops = flow.ops
        top_y = flow.y
        # Pos/Menge/Einheit linksbündig, Geldspalten rechtsbündig – oben (top) ausgerichtet
        text(ops, COL_POS, top_y, str(it.get('pos') or ''), size=9)
        text(ops, COL_QTY, top_y, f"{(it.get('quantity') or 0):.2f}".replace('.', ','), size=9)
        text(ops, COL_UNIT, top_y, str(it.get('unit') or ''), size=9)
        if per_item:
            text_right(ops, rate_right, top_y, _fmt_rate(it['rate']), size=9)
        text_right(ops, COL_PRICE_R, top_y, _price_text(it, currency), size=9)
        text_right(ops, COL_TOTAL_R, top_y, f"{_fmt_money(it.get('total') or 0)} {currency}", size=9)
        ly = top_y
        for ln in desc_lines:
            text(ops, COL_DESC, ly, ln, size=9)
            ly -= _ROW_LEADING
        flow.y -= row_h

    # Summenblock samt Hinweis zusammenhalten: passt er nicht mehr, neue Seite (ohne Header).
    note_lines = _note_lines(tax_note, RIGHT - LEFT, _NOTE_SIZE) if tax_note else []
    note_drop = _NOTE_TOP + _NOTE_LEADING * (len(note_lines) - 1) if note_lines else 0
    extra_tax_rows = max(len(tax_groups) - 1, 0) if per_item else 0
    if flow.y - (_TOTALS_HEIGHT + _TAX_ROW * extra_tax_rows + note_drop) < flow.bottom:
        flow.new_page()

    ops = flow.ops
    y = flow.y - 6
    flow.line_ops.append(f"{COL_DESC} {y + 4:.1f} m {RIGHT} {y + 4:.1f} l S")
    y -= 9
    if show_tax:
        # Netto- und MwSt-Zeile nur bei Steuerausweis. Bei Kleinunternehmern
        # (§19 UStG) entfällt jeder Steuerausweis – dann nur der Gesamtbetrag.
        text(ops, COL_DESC, y, "Summe netto:", size=10)
        text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(sum_net)} {currency}", font='/F2', size=10)
        y -= 14
        if per_item:
            # Je Satzgruppe eine Zeile; der Abstand zum Gesamtbetrag bleibt der heutige (14 + 2 = 16). Passt der Block nicht auf eine Seite,
            # geht es auf der nächsten mit der Zeile weiter, die nicht mehr passt; mit der letzten Gruppenzeile müssen der Gesamtbetrag
            # (+ 2), die Schlusslinie (+ 6) und der Hinweis noch auf die Seite – so steht der Gesamtbetrag nie allein
            for index, group in enumerate(tax_groups):
                below = _TAX_ROW + 8 + note_drop if index == len(tax_groups) - 1 else 0
                if y - below < flow.bottom:
                    flow.new_page()
                    ops = flow.ops
                    y = flow.y - 15
                if group.pct in exempt_rates:
                    caption = f"{group_label(group.pct, True)} auf {format_de(group.net)} {currency}:"
                else:
                    caption = f"zzgl. {_fmt_rate(group.pct)} MwSt. auf {format_de(group.net)} {currency}:"
                text(ops, COL_DESC, y, caption, size=10)
                text_right(ops, COL_TOTAL_R, y, f"{format_de(group.tax)} {currency}", font='/F2', size=10)
                y -= _TAX_ROW
            y -= 2
        else:
            tax_pct = (f"{tax_rate_pct:g}" if isinstance(tax_rate_pct, float) else str(tax_rate_pct))
            text(ops, COL_DESC, y, f"zzgl. {tax_pct}% MwSt.:", size=10)
            text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(tax_amount)} {currency}", font='/F2', size=10)
            y -= 16
    text(ops, COL_DESC, y, "Gesamtbetrag:", font='/F2', size=11)
    # Wert in size 10 wie netto/MwSt darüber, damit die Kommas exakt untereinander stehen
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(sum_gross)} {currency}", font='/F2', size=10)
    y -= 6
    flow.line_ops.append(f"{COL_DESC} {y:.1f} m {RIGHT} {y:.1f} l S")
    for number, line in enumerate(note_lines):
        text(ops, LEFT, y - _NOTE_TOP - _NOTE_LEADING * number, line, size=_NOTE_SIZE)
    y -= note_drop
    flow.y = y - 16


# ── Fließtext (Einleitungs-/Schlusstext, Phase 1: fett/kursiv + Umbruch) ───────

def _richtext_paragraphs(html_text):
    """contenteditable-HTML in Absätze aus (text, bold, italic)-Runs zerlegen.

    Der Text läuft zuerst durch die Positivliste (``richtext.sanitize_richtext``): Altbestand
    und Importe kommen roh aus der Datenbank, und reiner Text mit einem ``<`` (Zahlungstext
    „<14 Tagen“) bliebe sonst als vermeintliches Tag auf der Strecke."""
    from richtext import sanitize_richtext
    html_text = sanitize_richtext(html_text)
    if not html_text:
        return []
    t = re.sub(r'(?i)<\s*br\s*/?>', '\n', html_text)
    t = re.sub(r'(?i)<\s*li[^>]*>', '• ', t)
    t = re.sub(r'(?i)</\s*(p|div|li)\s*>', '\n', t)
    t = re.sub(r'(?i)<\s*(p|div|ul|ol)[^>]*>', '', t)
    paragraphs = []
    for raw in t.split('\n'):
        runs = []
        bold = italic = 0
        pos = 0
        for m in re.finditer(r'(?i)<\s*(/?)(b|strong|i|em)\s*>', raw):
            seg = raw[pos:m.start()]
            if seg:
                runs.append((_htmlmod.unescape(re.sub(r'<[^>]+>', '', seg)), bold > 0, italic > 0))
            closing = m.group(1) == '/'
            tag = m.group(2).lower()
            if tag in ('b', 'strong'):
                bold += -1 if closing else 1
            else:
                italic += -1 if closing else 1
            pos = m.end()
        seg = raw[pos:]
        if seg:
            runs.append((_htmlmod.unescape(re.sub(r'<[^>]+>', '', seg)), bold > 0, italic > 0))
        paragraphs.append(runs)
    return paragraphs


def richtext_to_lines(html_text, *, x=LEFT, max_x=RIGHT, size=10):
    """Fließtext in fertige Zeilen zerlegen (Grundlage für seitenübergreifenden
    Satz). Liefert Liste von ``(line_tokens, is_paragraph_end)``.
    """
    space_w = text_width(' ', size)
    out = []
    for runs in _richtext_paragraphs(html_text):
        words = [(w, b, i) for (txt, b, i) in runs for w in txt.split(' ')]
        line, line_w = [], 0.0
        for w, b, i in words:
            ww = text_width(w, size)
            if line and (x + line_w + ww > max_x):
                out.append((line, False))
                line, line_w = [], 0.0
            line.append((w, b, i))
            line_w += ww + space_w
        out.append((line, True))
    return out


def draw_text_lines(ops, lines, y, *, x=LEFT, size=10, leading=14, para_gap=4, min_y=None):
    """Vorbereitete Zeilen setzen. Bricht ab, sobald die nächste Zeile ``min_y``
    unterschreiten würde (Seitenumbruch). Returns ``(neues_y, restzeilen)``.
    """
    for idx, (line, is_end) in enumerate(lines):
        if min_y is not None and y < min_y:
            return y, lines[idx:]
        _flush_line(ops, line, x, y, size)
        y -= leading
        if is_end:
            y -= para_gap
    return y, []


def draw_richtext(ops, html_text, y, *, x=LEFT, max_x=RIGHT, size=10, leading=14):
    """Fließtext mit fett/kursiv und Wortumbruch setzen (einseitig). Returns y.

    Die Zeilenbreite dient nur der Umbruch-Entscheidung (Schätzung genügt). Die
    *Laufweite* übernimmt der PDF-Renderer selbst: pro Zeile wird die Text-Matrix
    einmal absolut gesetzt, danach schalten aufeinanderfolgende ``Tj`` den Cursor
    automatisch korrekt weiter – so kleben die Wörter nicht.
    """
    if not html_text:
        return y
    lines = richtext_to_lines(html_text, x=x, max_x=max_x, size=size)
    y, _ = draw_text_lines(ops, lines, y, x=x, size=size, leading=leading)
    return y


def _flush_line(ops, line, x, y, size):
    """Eine Zeile setzen: Matrix einmal absolut, dann Tj-Cursor-Autoadvance."""
    ops.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm")
    cur_font = None
    for w, b, i in line:
        font = '/F2' if b else ('/F3' if i else '/F1')
        if font != cur_font:
            ops.append(f"{font} {size} Tf")
            cur_font = font
        ops.append(f"({_esc(w)} ) Tj")   # Wort + Leerzeichen, Cursor läuft automatisch


# ── Seitenfluss (mehrseitiger Satz) ───────────────────────────────────────────

class PageFlow:
    """Verwaltet seitenübergreifenden Satz.

    Hält die aktuelle Seite (``ops`` im BT/ET-Textobjekt + ``line_ops`` für Linien
    danach) und die laufende Y-Position (``y``). Bei :meth:`new_page` wird die
    aktuelle Seite mit der dreispaltigen Fußzeile abgeschlossen und eine neue
    begonnen. ``finish`` liefert alle Seiten für :func:`build_multi_page_pdf`.

    Briefkopf/Positionen stehen typischerweise nur auf Seite 1; die Fußzeile
    erscheint auf jeder Seite.
    """

    def __init__(self, footer_cols, *, cont_top=790, bottom=120):
        self.footer_cols = footer_cols
        self.cont_top = cont_top      # Textbeginn auf Folgeseiten
        self.bottom = bottom          # Inhalt endet oberhalb der Fußzeile
        self.pages = []
        self.ops = ["BT"]
        self.line_ops = []
        self.y = None

    def _close(self):
        draw_footer_columns(self.ops, self.line_ops, self.footer_cols)
        self.ops.append("ET")
        self.ops.extend(self.line_ops)
        self.pages.append(self.ops)

    def new_page(self):
        """Aktuelle Seite abschließen (inkl. Fußzeile) und neue beginnen."""
        self._close()
        self.ops, self.line_ops = ["BT"], []
        self.y = self.cont_top
        return self.y

    def finish(self):
        """Letzte Seite abschließen und alle Seiten zurückgeben."""
        self._close()
        return self.pages


def flow_richtext(flow, html_text, *, size=10, leading=14, gap_before=0):
    """Fließtext seitenübergreifend in einen :class:`PageFlow` setzen."""
    if not html_text:
        return
    flow.y -= gap_before
    lines = richtext_to_lines(html_text, size=size)
    flow.y, remaining = draw_text_lines(flow.ops, lines, flow.y,
                                        size=size, leading=leading, min_y=flow.bottom)
    while remaining:
        flow.new_page()
        flow.y, remaining = draw_text_lines(flow.ops, remaining, flow.y,
                                            size=size, leading=leading, min_y=flow.bottom)
