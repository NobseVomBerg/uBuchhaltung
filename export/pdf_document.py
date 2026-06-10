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
from .pdf_core import escape_pdf_string

# A4 hochkant, Seitenränder
PAGE_W, PAGE_H = 595, 842
LEFT = 50
RIGHT = 545                       # rechter Rand (rechtsbündige Beträge)

# Positionstabellen-Spalten (X in pt); Beträge rechtsbündig an *_R
COL_POS = 50
COL_QTY_R = 150                   # Menge rechtsbündig
COL_UNIT = 165
COL_DESC = 215
COL_PRICE_R = 470                 # Einzelpreis rechtsbündig
COL_TOTAL_R = RIGHT               # Gesamt rechtsbündig

# Helvetica-Zeichenbreiten (em/1000) – grobe Mittelung genügt für Rechtsbündigkeit.
_DIGIT_W = 0.556
_AVG_W = 0.50


def _esc(s):
    return escape_pdf_string(str(s if s is not None else ''))


def text_width(s, size):
    """Grobe Textbreite in pt (Helvetica), ausreichend für Rechtsbündigkeit."""
    w = 0.0
    for ch in str(s):
        if ch.isdigit() or ch in '.,':
            w += _DIGIT_W
        elif ch in 'iljt.,;:\'! ':
            w += 0.28
        elif ch.isupper() or ch in 'mwMW':
            w += 0.70
        else:
            w += _AVG_W
    return w * size


def text(ops, x, y, s, font='/F1', size=10):
    """Linksbündige Textzelle an absoluter Position."""
    ops.append(f"{font} {size} Tf")
    ops.append(f"1 0 0 1 {x:.1f} {y:.1f} Tm ({_esc(s)}) Tj")


def text_right(ops, x_right, y, s, font='/F1', size=10):
    """Rechtsbündige Textzelle (endet bei x_right)."""
    text(ops, x_right - text_width(s, size), y, s, font, size)


# ── Briefkopf (wie Eingabeformular: Logo links, Meta rechts, Titel darunter) ──

# Meta-Block rechts: Label-Spalte / Wert-Spalte
META_LABEL_X = 390
META_VALUE_X = 470
LOGO_TOP = 800            # Oberkante Logo / Meta-Block


def draw_letterhead(ops, image, title, meta_rows, sender_line, address_lines):
    """Briefkopf zeichnen und y unterhalb des Titels zurückgeben.

    Aufbau (wie das Eingabeformular):
      - Logo oben **links**
      - Meta-Block (Datum, Nr., Kunden-Nr.) oben **rechts**, Label/Wert-Spalten
      - kleine Absenderzeile (kursiv) + Empfängeradresse links
      - Titel (fett) darunter
    """
    # Logo oben links (Bild-XObject; q/cm/Do/Q ist Grafik, hier vor dem Text)
    if image:
        w, h = image['width'], image['height']
        ops.append(f"q {w} 0 0 {h} {LEFT} {LOGO_TOP - h:.0f} cm /Logo Do Q")

    # Meta-Block oben rechts
    my = LOGO_TOP - 8
    for label, value in meta_rows:
        text(ops, META_LABEL_X, my, label, size=10)
        text(ops, META_VALUE_X, my, str(value), size=10)
        my -= 14

    # Absenderzeile + Empfängeradresse links (unterhalb des Logobereichs)
    y = 700
    if sender_line:
        text(ops, LEFT, y, sender_line, font='/F3', size=8)
    y -= 20
    for line in address_lines:
        if line:
            text(ops, LEFT, y, line, font='/F1', size=11)
            y -= 15

    # Titel unterhalb von Adresse UND Meta-Block
    y = min(y, my) - 16
    text(ops, LEFT, y, title, font='/F2', size=16)
    y -= 12
    return y


def draw_footer_columns(ops, line_ops, columns, y_top=96):
    """Dreispaltige Fußzeile am Seitenfuß (Anschrift | Kontakt | Bankverbindung).

    columns: Liste von 3 Zeilenlisten. Wird unten auf der Seite verankert, sodass
    sich die Trennlinie nicht verschiebt.
    """
    line_ops.append(f"{LEFT} {y_top + 8:.1f} m {RIGHT} {y_top + 8:.1f} l S")
    xs = [LEFT, 235, 410]
    for x, lines in zip(xs, columns):
        yy = y_top
        for ln in lines:
            if ln:
                text(ops, x, yy, ln, font='/F3', size=8)
            yy -= 11


# ── Positionstabelle ──────────────────────────────────────────────────────────

def _fmt_money(value):
    return f"{value:,.2f}".replace(',', '\x00').replace('.', ',').replace('\x00', '.')


def draw_item_table(ops, items, y, *, tax_rate_pct, sum_net, tax_amount, sum_gross,
                    currency='EUR'):
    """Positionstabelle mit rechtsbündigen Beträgen + Summenblock.

    items: Liste von Dicts mit pos, quantity, unit, description, price, total.
    Returns: y unterhalb des Summenblocks.
    """
    y -= 14
    # Kopfzeile
    text(ops, COL_POS, y, "Pos.", font='/F2', size=10)
    text_right(ops, COL_QTY_R, y, "Menge", font='/F2', size=10)
    text(ops, COL_UNIT, y, "Einheit", font='/F2', size=10)
    text(ops, COL_DESC, y, "Bezeichnung", font='/F2', size=10)
    text_right(ops, COL_PRICE_R, y, "Einzelpreis", font='/F2', size=10)
    text_right(ops, COL_TOTAL_R, y, "Gesamt", font='/F2', size=10)
    y -= 4
    rule_ops = [f"{LEFT} {y:.1f} m {RIGHT} {y:.1f} l S"]
    y -= 13

    # Bezeichnung darf nicht in die Einzelpreis-Spalte laufen: nach gemessener
    # Breite kürzen (Platz zwischen Bezeichnungsspalte und Einzelpreis-Spalte).
    desc_max_w = (COL_PRICE_R - 62) - COL_DESC
    for it in items:
        desc = str(it.get('description') or '')
        if text_width(desc, 9) > desc_max_w:
            while desc and text_width(desc + '...', 9) > desc_max_w:
                desc = desc[:-1]
            desc = desc.rstrip() + '...'
        text(ops, COL_POS, y, str(it.get('pos') or ''), size=9)
        text_right(ops, COL_QTY_R, y, f"{(it.get('quantity') or 0):.2f}".replace('.', ','), size=9)
        text(ops, COL_UNIT, y, str(it.get('unit') or ''), size=9)
        text(ops, COL_DESC, y, desc, size=9)
        text_right(ops, COL_PRICE_R, y, f"{_fmt_money(it.get('price') or 0)} {currency}", size=9)
        text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(it.get('total') or 0)} {currency}", size=9)
        y -= 13

    # Summenblock (Label rechtsbündig links der Beträge, mit Abstand)
    label_r = COL_PRICE_R - 18
    y -= 6
    rule_ops.append(f"{COL_DESC} {y + 4:.1f} m {RIGHT} {y + 4:.1f} l S")
    y -= 9
    text_right(ops, label_r, y, "Summe netto:", size=10)
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(sum_net)} {currency}", font='/F2', size=10)
    y -= 14
    tax_pct = (f"{tax_rate_pct:g}" if isinstance(tax_rate_pct, float) else str(tax_rate_pct))
    text_right(ops, label_r, y, f"zzgl. {tax_pct}% MwSt.:", size=10)
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(tax_amount)} {currency}", font='/F2', size=10)
    y -= 16
    text_right(ops, label_r, y, "Gesamtbetrag:", font='/F2', size=11)
    text_right(ops, COL_TOTAL_R, y, f"{_fmt_money(sum_gross)} {currency}", font='/F2', size=11)
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


def draw_richtext(ops, html_text, y, *, x=LEFT, max_x=RIGHT, size=10, leading=14):
    """Fließtext mit fett/kursiv und Wortumbruch setzen. Returns neues y.

    Die Zeilenbreite dient nur der Umbruch-Entscheidung (Schätzung genügt). Die
    *Laufweite* übernimmt der PDF-Renderer selbst: pro Zeile wird die Text-Matrix
    einmal absolut gesetzt, danach schalten aufeinanderfolgende ``Tj`` den Cursor
    automatisch korrekt weiter – so kleben die Wörter nicht.
    """
    if not html_text:
        return y
    space_w = text_width(' ', size)
    for runs in _richtext_paragraphs(html_text):
        words = []
        for txt, b, i in runs:
            for w in txt.split(' '):
                words.append((w, b, i))
        line, line_w = [], 0.0
        for w, b, i in words:
            ww = text_width(w, size)
            if line and (x + line_w + ww > max_x):
                _flush_line(ops, line, x, y, size)
                y -= leading
                line, line_w = [], 0.0
            line.append((w, b, i))
            line_w += ww + space_w
        if line:
            _flush_line(ops, line, x, y, size)
            y -= leading
        y -= 4   # Absatzabstand
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
