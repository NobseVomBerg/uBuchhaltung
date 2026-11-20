# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Stundenzettel-PDF (Arbeitszeiten je Person, typisch ein Monat).

Nutzt die gemeinsamen Primitive aus :mod:`export.pdf_core`. Bei einem vollen Monat
werden – wie auf der Seite – alle Tage gelistet, auch ohne Eintrag (``with_empty_days``, Standard;
ohne stehen nur die Tage mit Einträgen da); das passt meist auf
eine A4-Seite. Reicht der Platz nicht, läuft die Tabelle auf Folgeseiten weiter: Kopf, Logo
und Tabellenkopf stehen auf jeder Seite, bei mehr als einer Seite auch „Seite n von m“. Unter
der Tabelle stehen Summe und zwei Unterschriftenblöcke, zusammen auf der letzten Seite.

Mit einem Kundenfilter (``customer``) kommen nur die Einträge auf den Zettel, die
``entry_matches_customer`` – dieselbe Regel wie auf der Seite – dazu zählt; Kopf (``Kunde: …``), rechter
Unterschriftenblock und Dateiname nennen den Kunden.

Die Info-Spalte nennt die Art des Tages (Urlaub, Feiertag …), auf Wunsch den Arbeitsort
(``with_location``), die Pausenzeiten und auf Wunsch die Notiz (``with_notes``). Was nicht in
eine Zeile passt, bricht in der Spalte um (``info_lines``, gemessen mit den Breiten der
eingebetteten Schrift) – die Zeile wird entsprechend höher, und die Seitenverteilung rechnet mit
Textzeilen statt mit Tabellenzeilen.
"""
import datetime
import functools
import os
import re
from db import Database
from .pdf_core import (escape_pdf_string, load_image_xobject, build_multi_page_pdf, logo_display_size,
                       resolve_logo_path, to_winansi_bytes)
from server.pages_worktime import (compute_hours, compute_minutes, crosses_midnight, entry_matches_customer,
                                   format_hours, workdays_label, CUSTOMER_ALL, CUSTOMER_NONE, KIND_LABELS, WEEKDAYS)

# Spaltenindizes WorkTimes-Row (siehe pages_worktime)
#   0 ID,1 PersonID,2 Date,3 Kind,4 CustomerID,5 StartTime,6 EndTime,
#   7 PauseMinutes,8 LocationMode,9 LocationCity,10 Note,11 PauseText

MONTHS_DE = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
             'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember']

# Spalten-X-Positionen (pt)
COL_X = {
    'wd':    40,
    'date':  70,
    'start': 112,
    'end':   152,    # bis 'pause' Platz für „02:00 (+1)“ (36,7 pt bei 8 pt Schrift)
    'pause': 200,
    'hours': 238,   # rechtsbündig
    'info':  268,
}
ROW_H = 13
ROW_SIZE = 8
TABLE_TOP = 738          # Y der ersten Datenzeile
HEADER_Y = 751
BOTTOM_Y = 60            # keine Datenzeile darunter; auch der Unterschriftenblock endet nicht tiefer
PAGE_NUMBER_Y = 40       # „Seite n von m“
PAGE_RIGHT_X = 555       # rechter Rand (A4 595 pt, links stehen die Spalten ab 40 pt)
CUSTOMER_LINE_Y = 765    # „Kunde: …“ unter der Zeile „Zeitraum“ (nur mit Kundenfilter), vor dem Tabellenkopf bei HEADER_Y
CUSTOMER_LINE_SIZE = 9
CUSTOMER_LINE_WIDTH = 380      # ab x = 40 – das Logo beginnt bei x = 430
CUSTOMER_LINE_CHARS = 200      # so viele Zeichen des Namens werden höchstens betrachtet

# Unter der letzten Datenzeile: Summenzeile, dann der Unterschriftenblock (Ort/Datum, Linie, Name, Firma)
SUM_GAP = 3                    # Summenzeile unter dem Platz der nächsten Datenzeile
SIGNATURE_GAP = 34             # Ort/Datum des Unterschriftenblocks unter der Summenzeile
SIGNATURE_LINE_DROP = 38       # Unterschriftslinie unter Ort/Datum (Platz zum Unterschreiben)
SIGNATURE_NAME_DROP = 12       # Name unter der Linie
SIGNATURE_COMPANY_DROP = 24    # Firma unter der Linie: die tiefste Zeile des Blocks
FOOTER_HEIGHT = SUM_GAP + SIGNATURE_GAP + SIGNATURE_LINE_DROP + SIGNATURE_COMPANY_DROP


INFO_WIDTH = PAGE_RIGHT_X - COL_X['info']     # Breite der Info-Spalte bis zum rechten Rand
MAX_INFO_LINES = 8                            # höchstens so viele Textzeilen je Eintrag; danach steht „…“
LINES_PER_PAGE = int((TABLE_TOP - BOTTOM_Y) // ROW_H) + 1
_WORD = re.compile(r'\S+')


def page_layout(rows):
    """Datenzeilen je Seite → Liste mit der Zeilenzahl jeder Seite (mindestens eine Seite).

    ``rows`` ist die Zahl der Datenzeilen (jede eine Textzeile hoch) oder die Liste ihrer Höhen in
    Textzeilen (ein Eintrag mit umbrochener Info-Spalte ist mehrere hoch). Eine Datenzeile wird nie
    über zwei Seiten geteilt, und keine Textzeile steht unter ``BOTTOM_Y``. Summenzeile und
    Unterschriftenblock stehen zusammen auf der letzten Seite; reicht der Platz unter der letzten
    Datenzeile dafür nicht, folgt noch eine Seite ohne Datenzeilen."""
    heights = [1] * rows if isinstance(rows, int) else [max(1, min(int(h), LINES_PER_PAGE)) for h in rows]
    counts, used, count = [], 0, 0
    for height in heights:
        if used + height > LINES_PER_PAGE:
            counts.append(count)
            used = count = 0
        used += height
        count += 1
    counts.append(count)
    if TABLE_TOP - used * ROW_H - FOOTER_HEIGHT < BOTTOM_Y:
        counts.append(0)
    return counts


@functools.lru_cache(maxsize=1)
def _glyph_widths():
    """Breite jedes druckbaren Zeichens (cp1252) der eingebetteten Schrift in 1/1000 Geviert – aus der Schrift selbst
    (``pdfa.FONT_FILES['/F1']``), nicht aus der Helvetica-Tabelle: Dort fehlen Zeichen (ein Geviertstrich ist fast doppelt so
    breit wie der Ersatzwert), und einzelne weichen ab (``ß``, ``·``)."""
    from fontTools.ttLib import TTFont
    from . import pdfa
    font = TTFont(os.path.join(os.path.dirname(os.path.abspath(pdfa.__file__)), 'fonts', pdfa.FONT_FILES['/F1'][0]))
    units, cmap, metrics = font['head'].unitsPerEm, font.getBestCmap(), font['hmtx']
    widths = {}
    for code in range(32, 256):
        try:
            char = bytes([code]).decode('cp1252')
        except UnicodeDecodeError:
            continue
        glyph = cmap.get(ord(char))
        if glyph:
            widths[char] = metrics[glyph][0] * 1000.0 / units
    return widths


def printed_text(text):
    """Der Text, wie er im PDF steht: Zeichen außerhalb von WinAnsi ersetzt (``to_winansi_bytes``), ein unbekanntes
    als ``?``. Umbrochen und gemessen wird dieser Text – sonst zählte ein Pfeil als ein Zeichen und stünde als drei da."""
    return to_winansi_bytes(str(text or '')).decode('cp1252')


def info_width(text, size=ROW_SIZE):
    """Breite eines gedruckten Textes in pt (Zeichen ohne bekannte Breite zählen ein ganzes Geviert)."""
    widths = _glyph_widths()
    return sum(widths.get(char, 1000.0) for char in text) * size / 1000.0


def info_lines(text, max_width=INFO_WIDTH, size=ROW_SIZE, max_lines=MAX_INFO_LINES):
    """Den Text der Info-Spalte in Zeilen brechen: am Wortrand, ein Wort breiter als die Spalte mitten im Wort.
    Leerraum und Zeilenumbrüche zählen als ein Leerzeichen. Nach ``max_lines`` Zeilen ist Schluss; die letzte endet
    dann mit „…“ – nichts fällt weg, ohne dass man es sieht. Ein leerer Text hat keine Zeile.

    Die Laufzeit hängt nur von den Zeilen ab, die gedruckt werden, nicht von der Länge des Textes: Jedes Zeichen wird
    höchstens zweimal gemessen, und nach ``max_lines`` + 1 Zeilen hört das Lesen auf (die Notiz ist ein unbegrenztes
    Textfeld – ein Megabyte ohne Leerzeichen darf den Server nicht beschäftigen)."""
    widths = _glyph_widths()
    scale = size / 1000.0
    space = widths.get(' ', 1000.0) * scale
    lines, current, current_width = [], '', 0.0
    for match in _WORD.finditer(printed_text(text)):
        word, position = match.group(), 0
        while position < len(word) and len(lines) <= max_lines:
            # so viel vom Rest des Worts, wie hinter dem bisherigen Inhalt der Zeile noch Platz hat
            available = max_width - current_width - (space if current else 0.0)
            used, stop = 0.0, position
            while stop < len(word):
                step = widths.get(word[stop], 1000.0) * scale
                if used + step > available:
                    break
                used += step
                stop += 1
            if stop == len(word):                    # der Rest passt in die Zeile
                piece = word[position:]
                current_width += used + (space if current else 0.0)
                current = f'{current} {piece}' if current else piece
                position = stop
            elif current:                            # passt nicht mehr: neue Zeile, derselbe Rest noch einmal
                lines.append(current)
                current, current_width = '', 0.0
            else:                                    # allein breiter als die Spalte: mitten im Wort brechen
                stop = max(stop, position + 1)
                lines.append(word[position:stop])
                position = stop
        if len(lines) > max_lines:
            break
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and info_width(last + ' …', size) > max_width:
            last = last[:-1]
        lines = lines[:max_lines - 1] + [last.rstrip() + ' …']
    return lines


def customer_line(name, size=CUSTOMER_LINE_SIZE, max_width=CUSTOMER_LINE_WIDTH):
    """Die Kopfzeile „Kunde: <Name>“, auf ``max_width`` pt gekürzt (mit „…“), damit der Name nicht in das Logo läuft. Gemessen wird
    der Text, wie er gedruckt wird (``printed_text``, ``info_width``: die Breiten der eingebetteten Schrift); Leerraum und
    Zeilenumbrüche im Namen zählen als ein Leerzeichen. Die Laufzeit hängt nicht von der Länge des Namens ab: Betrachtet werden
    höchstens die ersten ``CUSTOMER_LINE_CHARS`` Zeichen, jedes einmal."""
    raw = str(name or '')
    text = 'Kunde: ' + ' '.join(printed_text(raw[:CUSTOMER_LINE_CHARS]).split())
    widths = _glyph_widths()
    scale = size / 1000.0
    ellipsis = widths.get('…', 1000.0) * scale
    total, fit = 0.0, 0                       # fit: so viele Zeichen bleiben, wenn hinter ihnen noch das „…“ Platz hat
    for count, char in enumerate(text, start=1):
        total += widths.get(char, 1000.0) * scale
        if total + ellipsis <= max_width:
            fit = count
    if total <= max_width and len(raw) <= CUSTOMER_LINE_CHARS:
        return text
    return text[:fit].rstrip() + '…'


def _fmt_hours(h):
    return f'{h:.2f}'.replace('.', ',')


FILE_PART_LENGTH = 80        # Zeichen eines Namens im Dateinamen (ein Kontaktname hat keine Längengrenze)


def _file_part(name):
    """Ein Name als Teil des Dateinamens: nur Buchstaben, Ziffern, Leerzeichen (zu ``_``), ``-`` und ``_`` bleiben, höchstens
    ``FILE_PART_LENGTH`` Zeichen – ein überlanger Name ergäbe einen Antwort-Kopf, den der Browser ablehnt."""
    kept = "".join(ch for ch in str(name)[:10 * FILE_PART_LENGTH] if ch.isalnum() or ch in (' ', '-', '_'))
    return kept.strip()[:FILE_PART_LENGTH].strip().replace(' ', '_')


def _cell(x, y, text, font='/F1', size=ROW_SIZE, right_x=None, gray=False):
    """Eine Textzelle an absoluter Position (Text-Matrix). gray=True → graue Schrift."""
    txt = escape_pdf_string(str(text))
    ops = []
    if gray:
        ops.append("0.5 g")
    ops.append(f"{font} {size} Tf")
    if right_x is not None:
        approx = len(str(text)) * size * 0.5      # grobe Rechtsbündigkeit
        x = right_x - approx
    ops.append(f"1 0 0 1 {x:.1f} {y} Tm ({txt}) Tj")
    if gray:
        ops.append("0 g")
    return ops


def _signature_block(ops, line_ops, x, y_top, place_date, name, company, width=210):
    """Unterschriftenblock: Ort+Datum oben, Unterschriftslinie, darunter Name + Firma."""
    if place_date:
        ops += _cell(x, y_top, place_date, size=8)
    y_line = y_top - SIGNATURE_LINE_DROP           # Linie (darüber Platz zum Unterschreiben)
    line_ops.append(f"{x} {y_line:.1f} m {x + width} {y_line:.1f} l S")
    if name:
        ops += _cell(x, y_line - SIGNATURE_NAME_DROP, name, size=8)
    if company:
        ops += _cell(x, y_line - SIGNATURE_COMPANY_DROP, company, size=8)


def _work_minutes(e):
    """Arbeitsminuten eines Eintrags; 0 für Urlaub, Krank, Feiertag und einen Tag ohne Eintrag."""
    if e is None or (e[3] or 'work') != 'work':
        return 0
    return compute_minutes(e[5], e[6], e[7])


def _head_ops(image, person_name, own_name, period_label, customer_text=None):
    """Kopf einer Seite: Logo, Titel, Person, Zeitraum, bei einem Kundenfilter der Kunde (``customer_text``, siehe
    ``customer_line``) und Tabellenkopf."""
    ops = []
    if image:
        dw, dh = logo_display_size(image, 150, 80)
        ops.append(f"q {dw:.1f} 0 0 {dh:.1f} 430 770 cm /Logo Do Q")

    ops += _cell(40, 805, "Stundenzettel", font='/F2', size=14)
    person_line = f"Person: {person_name}" + (f", {own_name}" if own_name else "")
    ops += _cell(40, 789, person_line, size=10)
    ops += _cell(40, 777, f"Zeitraum: {period_label}", size=9)
    if customer_text:
        ops += _cell(40, CUSTOMER_LINE_Y, customer_text, size=CUSTOMER_LINE_SIZE)

    header = [('wd', 'Tag'), ('date', 'Datum'), ('start', 'Start'), ('end', 'Ende'),
              ('pause', 'Pause'), ('hours', 'Std.'), ('info', 'Info')]
    for key, label in header:
        if key == 'hours':
            ops += _cell(0, HEADER_Y, label, font='/F2', size=8, right_x=COL_X['hours'] + 18)
        else:
            ops += _cell(COL_X[key], HEADER_Y, label, font='/F2', size=8)
    return ops


def generate_worktime_pdf(db: Database, person_id, date_from, date_to, with_notes=False,
                          store=True, with_location=False, customer=CUSTOMER_ALL, with_empty_days=True):
    """Stundenzettel-PDF erzeugen. Returns (pdf_bytes, pdf_path) oder (None, None).

    ``with_notes`` druckt die Notiz jedes Eintrags in die Info-Spalte, ``with_location`` den
    Arbeitsort eines Arbeitstags (wie die Liste der Seite ihn unter „Info“ zeigt).

    ``customer`` ist der Kundenfilter (``customer_filter``: ``''`` alle, ``'none'`` nur Einträge ohne Kunde, sonst die ID
    eines Kunden; welche Einträge dazugehören, sagt ``entry_matches_customer``). Mit Filter nennen Kopf, rechter
    Unterschriftenblock (bei einem Kunden, auch ohne Arbeitseintrag im Zeitraum) und Dateiname den Kunden.
    ``with_empty_days=False`` lässt im Monatszettel die Tage ohne Eintrag weg.

    ``store=False`` legt nichts auf der Platte ab (so liefert der Download unter ``/worktime/pdf``,
    ein GET, den Stundenzettel aus, ohne zu schreiben); ``pdf_path`` nennt dann nur, wo die Datei
    läge – daraus entsteht der Dateiname des Downloads.
    """
    if not person_id:
        return None, None

    person = db.get_contact_by_id(person_id)
    person_name = (person[3] if person else None) or f'Person {person_id}'
    person_city = (person[7] if person else '') or ''

    own_rows = list(db.fetch_contacts(contact_type='own'))
    own = own_rows[0] if own_rows else None
    own_name = (own[3] if own else '') or ''
    own_city = (own[7] if own else '') or ''
    if not person_city:
        person_city = own_city
    image = load_image_xobject(resolve_logo_path(own[13])) if (own and own[13]) else None

    d_from = datetime.date.fromisoformat(date_from)
    d_to = datetime.date.fromisoformat(date_to)
    single_month = (d_from.year == d_to.year and d_from.month == d_to.month)

    # der Kundenfilter wirkt vor allem anderen: Zeilen, Summe, Arbeitstage und Unterschrift rechnen mit den gefilterten
    entries = [e for e in db.fetch_worktimes(person_id, date_from, date_to) if entry_matches_customer(e, customer)]
    by_date = {}
    for e in entries:
        by_date.setdefault(str(e[2]), []).append(e)

    # Rechter Unterschriftenblock: Mit Kundenfilter dieser Kunde (auch ohne Arbeitseintrag im Zeitraum), sonst der eine
    # Kunde, den die Einträge nennen. „Ohne Kunde“ hat keinen: Die gefilterten Einträge nennen keinen.
    if isinstance(customer, int):
        single_customer = db.get_contact_by_id(customer)
    else:
        cust_ids = {e[4] for e in entries if e[4]}
        single_customer = db.get_contact_by_id(next(iter(cust_ids))) if len(cust_ids) == 1 else None

    # Name für Kopf und Dateiname; ``None`` ohne Filter
    if customer == CUSTOMER_NONE:
        customer_name = 'ohne Kunde'
    elif isinstance(customer, int):
        customer_name = (single_customer[3] if single_customer else '') or f'ID {customer}'
    else:
        customer_name = None
    customer_text = customer_line(customer_name) if customer_name is not None else None

    if single_month:
        period_label = f'{MONTHS_DE[d_from.month - 1]} {d_from.year}'
    else:
        period_label = f'{d_from.strftime("%d.%m.%Y")} - {d_to.strftime("%d.%m.%Y")}'

    # ── Zeilen der Tabelle: (Tag, Eintrag); ein Tag ohne Eintrag hat None ────
    rows = []
    if single_month:
        for day in range(d_from.day, d_to.day + 1):
            d = datetime.date(d_from.year, d_from.month, day)
            day_entries = by_date.get(d.isoformat(), [])
            if day_entries:
                rows += [(d, e) for e in day_entries]
            elif with_empty_days:
                rows.append((d, None))
    else:
        rows = [(datetime.date.fromisoformat(str(e[2])), e) for e in entries]

    # Summe aus Minuten (nie aus den gerundeten Stunden der Zeilen); Arbeitstage = Kalendertage
    total_minutes = 0
    workdays = set()
    for d, e in rows:
        minutes = _work_minutes(e)
        if minutes > 0:
            total_minutes += minutes
            workdays.add(d)

    def row_info(e):
        """Die Textzeilen der Info-Spalte: Art (Urlaub/Feiertag), auf Wunsch der Arbeitsort,
        Pausenzeiten, auf Wunsch die Notiz – umbrochen statt abgeschnitten."""
        if e is None:
            return []
        kind = e[3] or 'work'
        pause_text = e[11] if len(e) > 11 else ''
        parts = []
        if kind != 'work':
            parts.append(KIND_LABELS.get(kind, kind))
        if with_location and kind == 'work' and e[9]:
            parts.append(str(e[9]))
        if pause_text:
            parts.append('Pause ' + str(pause_text))
        if with_notes and e[10]:
            parts.append(str(e[10]))
        return info_lines(' · '.join(part.strip() for part in parts if part.strip()))

    def render_row(d, e, y, info):
        row = []
        row += _cell(COL_X['wd'], y, WEEKDAYS[d.weekday()])
        row += _cell(COL_X['date'], y, d.strftime('%d.%m.'))
        if e is None:
            return row
        kind = e[3] or 'work'
        start = e[5] or ''
        end = e[6] or ''
        pause = e[7] or 0
        hours = compute_hours(start, end, pause) if kind == 'work' else 0.0

        row += _cell(COL_X['start'], y, start)
        row += _cell(COL_X['end'], y, f'{end} (+1)' if crosses_midnight(start, end) else end)
        row += _cell(COL_X['pause'], y, str(pause) if (kind == 'work' and pause) else '')
        hours_txt = _fmt_hours(hours) if _work_minutes(e) > 0 else ''
        row += _cell(0, y, hours_txt, right_x=COL_X['hours'] + 18)
        for number, line in enumerate(info):
            row += _cell(COL_X['info'], y - number * ROW_H, line, gray=True)
        return row

    # ── Seiten (Content-Stream: Text in BT/ET, Linien danach) ────────────────
    today_str = datetime.date.today().strftime('%d.%m.%Y')
    # nie höher als eine Seite – dieselbe Grenze, mit der ``page_layout`` rechnet
    infos = [row_info(e)[:LINES_PER_PAGE] for _, e in rows]
    heights = [max(1, len(info)) for info in infos]
    counts = page_layout(heights)
    pages = []
    first = 0
    for number, count in enumerate(counts, start=1):
        ops = ["BT"] + _head_ops(image, person_name, own_name, period_label, customer_text)
        line_ops = []

        y = TABLE_TOP
        for index in range(first, first + count):
            d, e = rows[index]
            ops += render_row(d, e, y, infos[index])
            y -= ROW_H * heights[index]
        first += count

        if number == len(counts):
            # Summenzeile: Label in der Tag-Spalte (ganz links), Summe rechtsbündig unter Std.
            y -= SUM_GAP
            ops += _cell(COL_X['wd'], y, f"Summe ({workdays_label(len(workdays))}):", font='/F2', size=8)
            ops += _cell(0, y, format_hours(total_minutes), font='/F2', size=8,
                         right_x=COL_X['hours'] + 18)

            # ── Unterschriftsbereiche ────────────────────────────────────────
            y_sig = y - SIGNATURE_GAP
            _signature_block(ops, line_ops, 40, y_sig,
                             f"{person_city}, den {today_str}" if person_city else f"den {today_str}",
                             person_name, own_name)
            if single_customer:
                c_city = single_customer[7] or ''
                c_name = single_customer[3] or ''
                _signature_block(ops, line_ops, 320, y_sig,
                                 f"{c_city}, den {today_str}" if c_city else f"den {today_str}",
                                 '', c_name)

        if len(counts) > 1:
            ops += _cell(0, PAGE_NUMBER_Y, f"Seite {number} von {len(counts)}", right_x=PAGE_RIGHT_X)

        ops.append("ET")
        ops += line_ops               # Linien außerhalb des Textobjekts zeichnen
        pages.append(ops)

    full_pdf = build_multi_page_pdf(pages, image=image)

    # ── Datei speichern (im Datenbereich des aktuellen Nutzers) ──────────────
    import userctx
    pdf_dir = os.path.join(userctx.user_data_dir(), "worktime", str(d_from.year))
    safe_name = _file_part(person_name)
    period_tag = f'{d_from.year}-{d_from.month:02d}' if single_month else f'{date_from}_{date_to}'
    if customer_name is None:
        pdf_filename = f"Stundenzettel_{safe_name}_{period_tag}.pdf"
    else:
        customer_part = 'ohne-Kunde' if customer == CUSTOMER_NONE else _file_part(customer_name)
        pdf_filename = f"Stundenzettel_{safe_name}_{customer_part}_{period_tag}.pdf"
    pdf_path = os.path.join(pdf_dir, pdf_filename)
    if store:
        try:
            os.makedirs(pdf_dir, exist_ok=True)
            with open(pdf_path, 'wb') as f:
                f.write(full_pdf)
        except Exception as ex:
            print(f"Error saving worktime PDF: {ex}")
            return None, None

    return full_pdf, pdf_path
