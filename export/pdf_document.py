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
from .pdf_core import escape_pdf_string, logo_display_size

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

# Meta-Block rechts: Label-Spalte / Wert-Spalte
META_LABEL_X = 390
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


def draw_letterhead(ops, image, title, meta_rows, sender_line, address_lines):
    """Briefkopf zeichnen und y unterhalb des Titels zurückgeben.

    Aufbau (wie das Eingabeformular):
      - Logo oben **links**
      - Meta-Block (Datum, Nr., Kunden-Nr.) oben **rechts**, Label/Wert-Spalten
      - kleine Absenderzeile (kursiv) + Empfängeradresse links
      - Titel (fett) rechtsbündig, beginnend an der Meta-Spalte
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
    return f"{value:,.2f}".replace(',', '\x00').replace('.', ',').replace('\x00', '.')


_ROW_LEADING = 13                 # Zeilenabstand innerhalb einer Position


def _wrap_description(desc, max_w, size):
    """Bezeichnung in Zeilen zerlegen: ``;`` → Zeilenumbruch, lange Zeilen am
    Wortrand umbrechen. Liefert immer mindestens eine (ggf. leere) Zeile."""
    lines = []
    for segment in str(desc or '').split(';'):
        seg = segment.strip()
        if not seg:
            continue
        cur = ''
        for word in seg.split(' '):
            trial = (cur + ' ' + word).strip()
            if cur and text_width(trial, size) > max_w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        if cur:
            lines.append(cur)
    return lines or ['']


def draw_item_table(ops, items, y, *, tax_rate_pct, sum_net, tax_amount, sum_gross,
                    currency='EUR'):
    """Positionstabelle mit rechtsbündigen Geldspalten + Summenblock.

    items: Liste von Dicts mit pos, quantity, unit, description, price, total.
    Bezeichnungen dürfen mehrzeilig sein (``;`` trennt Zeilen); die übrigen
    Spalten werden dann oben (top) ausgerichtet.
    Returns: (y unterhalb des Summenblocks, rule_ops).
    """
    y -= 14
    # Kopfzeile: erste vier Spalten links, Geldspalten rechtsbündig
    text(ops, COL_POS, y, "Pos.", font='/F2', size=10)
    text(ops, COL_QTY, y, "Menge", font='/F2', size=10)
    text(ops, COL_UNIT, y, "Einheit", font='/F2', size=10)
    text(ops, COL_DESC, y, "Bezeichnung", font='/F2', size=10)
    text_right(ops, COL_PRICE_R, y, "Einzelpreis", font='/F2', size=10)
    text_right(ops, COL_TOTAL_R, y, "Gesamt", font='/F2', size=10)
    y -= 4
    rule_ops = [f"{LEFT} {y:.1f} m {RIGHT} {y:.1f} l S"]
    y -= 13

    # Bezeichnung darf nicht in die Einzelpreis-Spalte laufen.
    desc_max_w = (COL_PRICE_R - 65) - COL_DESC
    for it in items:
        desc_lines = _wrap_description(it.get('description'), desc_max_w, 9)
        top_y = y
        # Pos/Menge/Einheit linksbündig, Geldspalten rechtsbündig – oben (top) ausgerichtet
        text(ops, COL_POS, top_y, str(it.get('pos') or ''), size=9)
        text(ops, COL_QTY, top_y, f"{(it.get('quantity') or 0):.2f}".replace('.', ','), size=9)
        text(ops, COL_UNIT, top_y, str(it.get('unit') or ''), size=9)
        text_right(ops, COL_PRICE_R, top_y, f"{_fmt_money(it.get('price') or 0)} {currency}", size=9)
        text_right(ops, COL_TOTAL_R, top_y, f"{_fmt_money(it.get('total') or 0)} {currency}", size=9)
        # Mehrzeilige Bezeichnung
        ly = top_y
        for ln in desc_lines:
            text(ops, COL_DESC, ly, ln, size=9)
            ly -= _ROW_LEADING
        y -= _ROW_LEADING * len(desc_lines)

    # Summenblock: Beschriftung linksbündig in der Bezeichnungs-Spalte,
    # Werte rechtsbündig in der Gesamt-Spalte.
    y -= 6
    rule_ops.append(f"{COL_DESC} {y + 4:.1f} m {RIGHT} {y + 4:.1f} l S")
    y -= 9
    text(ops, COL_DESC, y, "Summe netto:", size=10)
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(sum_net)} {currency}", font='/F2', size=10)
    y -= 14
    tax_pct = (f"{tax_rate_pct:g}" if isinstance(tax_rate_pct, float) else str(tax_rate_pct))
    text(ops, COL_DESC, y, f"zzgl. {tax_pct}% MwSt.:", size=10)
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(tax_amount)} {currency}", font='/F2', size=10)
    y -= 16
    text(ops, COL_DESC, y, "Gesamtbetrag:", font='/F2', size=11)
    # Wert in size 10 wie netto/MwSt darüber, damit die Kommas exakt untereinander stehen
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(sum_gross)} {currency}", font='/F2', size=10)
    y -= 6
    rule_ops.append(f"{COL_DESC} {y:.1f} m {RIGHT} {y:.1f} l S")
    y -= 16
    return y, rule_ops


# ── Fließtext (Einleitungs-/Schlusstext, Phase 1: fett/kursiv + Umbruch) ───────

def _richtext_paragraphs(html_text):
    """contenteditable-HTML in Absätze aus (text, bold, italic)-Runs zerlegen."""
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
