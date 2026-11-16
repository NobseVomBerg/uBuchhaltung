# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Stundenzettel-PDF (Arbeitszeiten je Person, typisch ein Monat).

Nutzt die gemeinsamen Primitive aus :mod:`export.pdf_core`. Bei einem vollen Monat
werden – wie auf der Seite – alle Tage gelistet, auch ohne Eintrag; das passt meist auf
eine A4-Seite. Reicht der Platz nicht, läuft die Tabelle auf Folgeseiten weiter: Kopf, Logo
und Tabellenkopf stehen auf jeder Seite, bei mehr als einer Seite auch „Seite n von m“. Unter
der Tabelle stehen Summe und zwei Unterschriftenblöcke, zusammen auf der letzten Seite.
"""
import datetime
import os
from db import Database
from .pdf_core import (escape_pdf_string, load_image_xobject, build_multi_page_pdf, logo_display_size,
                       resolve_logo_path)
from server.pages_worktime import (compute_hours, compute_minutes, crosses_midnight, format_hours,
                                   workdays_label, KIND_LABELS, WEEKDAYS)

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

# Unter der letzten Datenzeile: Summenzeile, dann der Unterschriftenblock (Ort/Datum, Linie, Name, Firma)
SUM_GAP = 3                    # Summenzeile unter dem Platz der nächsten Datenzeile
SIGNATURE_GAP = 34             # Ort/Datum des Unterschriftenblocks unter der Summenzeile
SIGNATURE_LINE_DROP = 38       # Unterschriftslinie unter Ort/Datum (Platz zum Unterschreiben)
SIGNATURE_NAME_DROP = 12       # Name unter der Linie
SIGNATURE_COMPANY_DROP = 24    # Firma unter der Linie: die tiefste Zeile des Blocks
FOOTER_HEIGHT = SUM_GAP + SIGNATURE_GAP + SIGNATURE_LINE_DROP + SIGNATURE_COMPANY_DROP


def page_layout(row_count):
    """Datenzeilen je Seite → Liste mit der Zeilenzahl jeder Seite (mindestens eine Seite).

    Keine Datenzeile steht unter ``BOTTOM_Y``. Summenzeile und Unterschriftenblock stehen
    zusammen auf der letzten Seite; reicht der Platz unter der letzten Datenzeile dafür nicht,
    folgt noch eine Seite ohne Datenzeilen."""
    per_page = int((TABLE_TOP - BOTTOM_Y) // ROW_H) + 1
    counts = []
    while row_count > per_page:
        counts.append(per_page)
        row_count -= per_page
    counts.append(row_count)
    if TABLE_TOP - row_count * ROW_H - FOOTER_HEIGHT < BOTTOM_Y:
        counts.append(0)
    return counts


def _fmt_hours(h):
    return f'{h:.2f}'.replace('.', ',')


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


def _head_ops(image, person_name, own_name, period_label):
    """Kopf einer Seite: Logo, Titel, Person, Zeitraum und Tabellenkopf."""
    ops = []
    if image:
        dw, dh = logo_display_size(image, 150, 80)
        ops.append(f"q {dw:.1f} 0 0 {dh:.1f} 430 770 cm /Logo Do Q")

    ops += _cell(40, 805, "Stundenzettel", font='/F2', size=14)
    person_line = f"Person: {person_name}" + (f", {own_name}" if own_name else "")
    ops += _cell(40, 789, person_line, size=10)
    ops += _cell(40, 777, f"Zeitraum: {period_label}", size=9)

    header = [('wd', 'Tag'), ('date', 'Datum'), ('start', 'Start'), ('end', 'Ende'),
              ('pause', 'Pause'), ('hours', 'Std.'), ('info', 'Info')]
    for key, label in header:
        if key == 'hours':
            ops += _cell(0, HEADER_Y, label, font='/F2', size=8, right_x=COL_X['hours'] + 18)
        else:
            ops += _cell(COL_X[key], HEADER_Y, label, font='/F2', size=8)
    return ops


def generate_worktime_pdf(db: Database, person_id, date_from, date_to, with_notes=False,
                          store=True):
    """Stundenzettel-PDF erzeugen. Returns (pdf_bytes, pdf_path) oder (None, None).

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

    entries = list(db.fetch_worktimes(person_id, date_from, date_to))
    by_date = {}
    for e in entries:
        by_date.setdefault(str(e[2]), []).append(e)

    # Genau ein Kunde im Zeitraum? → rechter Unterschriftenblock
    cust_ids = {e[4] for e in entries if e[4]}
    single_customer = db.get_contact_by_id(next(iter(cust_ids))) if len(cust_ids) == 1 else None

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
            else:
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

    def render_row(d, e, y):
        row = []
        row += _cell(COL_X['wd'], y, WEEKDAYS[d.weekday()])
        row += _cell(COL_X['date'], y, d.strftime('%d.%m.'))
        if e is None:
            return row
        kind = e[3] or 'work'
        start = e[5] or ''
        end = e[6] or ''
        pause = e[7] or 0
        pause_text = e[11] if len(e) > 11 else ''
        hours = compute_hours(start, end, pause) if kind == 'work' else 0.0

        # Info-Spalte: Art (Urlaub/Feiertag) + Pausenzeiten + optional Notiz
        parts = []
        if kind != 'work':
            parts.append(KIND_LABELS.get(kind, kind))
        if pause_text:
            parts.append('Pause ' + pause_text)
        if with_notes and e[10]:
            parts.append(e[10])
        info = ' · '.join(parts)[:70]

        row += _cell(COL_X['start'], y, start)
        row += _cell(COL_X['end'], y, f'{end} (+1)' if crosses_midnight(start, end) else end)
        row += _cell(COL_X['pause'], y, str(pause) if (kind == 'work' and pause) else '')
        hours_txt = _fmt_hours(hours) if _work_minutes(e) > 0 else ''
        row += _cell(0, y, hours_txt, right_x=COL_X['hours'] + 18)
        if info:
            row += _cell(COL_X['info'], y, info, gray=True)
        return row

    # ── Seiten (Content-Stream: Text in BT/ET, Linien danach) ────────────────
    today_str = datetime.date.today().strftime('%d.%m.%Y')
    counts = page_layout(len(rows))
    pages = []
    first = 0
    for number, count in enumerate(counts, start=1):
        ops = ["BT"] + _head_ops(image, person_name, own_name, period_label)
        line_ops = []

        y = TABLE_TOP
        for d, e in rows[first:first + count]:
            ops += render_row(d, e, y)
            y -= ROW_H
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
    safe_name = "".join(ch for ch in person_name if ch.isalnum() or ch in (' ', '-', '_')).strip().replace(' ', '_')
    period_tag = f'{d_from.year}-{d_from.month:02d}' if single_month else f'{date_from}_{date_to}'
    pdf_filename = f"Stundenzettel_{safe_name}_{period_tag}.pdf"
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
