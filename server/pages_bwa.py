# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Betriebswirtschaftliche Auswertung (``GET /bwa``, ``GET /bwa/pdf``).

Die Seite zeigt einen Monat und – aufgelaufen – die Zeit seit dem 1. Januar, wahlweise mit Anteilen oder im Vergleich mit dem
Vorjahr; unter jeder Zeile lassen sich ihre Konten aufklappen. Gerechnet wird in ``db.bwa`` (eine Umgruppierung der EÜR), hier
steht nur das Lesen der Adresse und das HTML. Die Seite schreibt nichts: kein Cookie, keine Einstellung, keine Datei.

Jeder Wert aus Datenbank oder Adresse steht maskiert im HTML (``html.escape``); die Daten des Skripts gehen über ``script_json``.
Gelesen wird die Adresse streng (``parse_year``, ``parse_month``, ``parse_variant``): ein Schlüssel, der mehrfach vorkommt, ist ungültig.
"""
import html as _html
from dataclasses import dataclass

from db import Database
from db.bwa import (DEFAULT_VARIANT, HINT_TEXT, INVALID_PERIOD_TEXT, MONTH_NAMES, OUTSIDE_HEADING, PERCENT_TEXT, SIGN_TEXT, TITLE,
                    UNASSIGNED_TEXT, VARIANT_LABELS, VARIANTS, BwaLine, BwaReport, block_headers, cells, column_labels,
                    default_month, month_label, visible, visible_accounts)
from .pages import Footer, Header1, Header2, Header3, script_json

MIN_YEAR, MAX_YEAR = 1900, 2999
INVALID_PERIOD_ERROR = 'Ungültiger Zeitraum.'
INVALID_VARIANT_ERROR = 'Ungültige Darstellung.'
PDF_FAILED_TEXT = 'Die Auswertung ließ sich nicht als PDF erstellen.'


@dataclass(frozen=True)
class BwaQuery:
    """Was die Adresse verlangt: Jahr, Monat und Darstellung; ``invalid``: Der angegebene Zeitraum ließ sich nicht lesen, es gilt der
    Vormonat (die Seite sagt es)."""
    year: int
    month: int
    variant: str
    invalid: bool = False


def parse_year(text) -> int | None:
    """Genau vier ASCII-Ziffern, 1900 bis 2999 – sonst ``None``."""
    if isinstance(text, str) and len(text) == 4 and text.isascii() and text.isdigit():
        value = int(text)
        if MIN_YEAR <= value <= MAX_YEAR:
            return value
    return None


def parse_month(text) -> int | None:
    """Ein oder zwei ASCII-Ziffern, 1 bis 12 (``1`` und ``01`` gelten; ``001``, ``+1``, ``1.0``, Leerraum und Ziffern anderer
    Schriften nicht) – sonst ``None``."""
    if isinstance(text, str) and 1 <= len(text) <= 2 and text.isascii() and text.isdigit():
        value = int(text)
        if 1 <= value <= 12:
            return value
    return None


def parse_variant(text) -> str | None:
    """Genau ``jahr`` oder ``vorjahr`` – sonst ``None``."""
    return text if isinstance(text, str) and text in VARIANTS else None


def _single(qs: dict, name: str) -> tuple[bool, str | None]:
    """``(angegeben, Wert)``: Ein Schlüssel, der mehrfach vorkommt, ist angegeben und hat keinen gültigen Wert."""
    values = qs.get(name)
    if not values:
        return False, None
    return True, values[0] if len(values) == 1 else None


def read_page_query(qs: dict, today) -> BwaQuery:
    """Die Adresse der Seite lesen. Ohne Jahr und Monat gilt der Vormonat; ist einer von beiden ungültig oder fehlt nur einer,
    ebenfalls – dann mit ``invalid`` (Hinweis auf der Seite). Eine fehlende oder ungültige Darstellung ist der Vorjahresvergleich,
    ohne Hinweis."""
    year_given, year_text = _single(qs, 'year')
    month_given, month_text = _single(qs, 'month')
    _, variant_text = _single(qs, 'variant')
    variant = parse_variant(variant_text) or DEFAULT_VARIANT
    year, month = parse_year(year_text), parse_month(month_text)
    if year is not None and month is not None:
        return BwaQuery(year, month, variant)
    default_year, default_month_number = default_month(today)
    return BwaQuery(default_year, default_month_number, variant, invalid=year_given or month_given)


def read_pdf_query(qs: dict) -> tuple[BwaQuery | None, str | None]:
    """Die Adresse des PDF lesen: ``(Abfrage, None)`` oder ``(None, Fehlertext)``. Jahr und Monat sind Pflicht; die Darstellung
    ist der Vorjahresvergleich, wenn sie fehlt, und ein Fehler, wenn sie ungültig ist."""
    _, year_text = _single(qs, 'year')
    _, month_text = _single(qs, 'month')
    year, month = parse_year(year_text), parse_month(month_text)
    if year is None or month is None:
        return None, INVALID_PERIOD_ERROR
    variant_given, variant_text = _single(qs, 'variant')
    if not variant_given:
        return BwaQuery(year, month, DEFAULT_VARIANT), None
    variant = parse_variant(variant_text)
    if variant is None:
        return None, INVALID_VARIANT_ERROR
    return BwaQuery(year, month, variant), None


def _esc(value) -> str:
    """Wert aus Datenbank oder Adresse für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


# Aufklappen der Konten unter einer Zeile: Die Konten sind Zeilen mit data-bwa-parent, das Symbol hat die ID bwa-icon-<Schlüssel>.
# Nur var und function, Werte nur über textContent und Eigenschaften.
BWA_JS = '''
function bwaPdf() {
  var box = document.getElementById('bwaAccounts');
  appDownload(BWA_DATA.pdf + '&accounts=' + (box && box.checked ? '1' : '0'));
}
function bwaSetGroup(key, open) {
  var rows = document.querySelectorAll('tr[data-bwa-parent="' + key + '"]');
  for (var i = 0; i < rows.length; i++) { rows[i].hidden = !open; }
  var icon = document.getElementById('bwa-icon-' + key);
  if (icon) { icon.textContent = open ? '\\u25BC' : '\\u25B6'; }
}
function bwaToggle(key) {
  var first = document.querySelector('tr[data-bwa-parent="' + key + '"]');
  bwaSetGroup(key, !!first && first.hidden);
}
function bwaToggleAll() {
  var icons = document.querySelectorAll('.split-toggle-icon[id^="bwa-icon-"]');
  var open = false;
  for (var i = 0; i < icons.length; i++) {
    var first = document.querySelector('tr[data-bwa-parent="' + icons[i].id.substring(9) + '"]');
    if (first && first.hidden) { open = true; }
  }
  for (var j = 0; j < icons.length; j++) { bwaSetGroup(icons[j].id.substring(9), open); }
}
'''


def _numbers(texts) -> str:
    return ''.join(f'<td class="num">{_esc(text)}</td>' for text in texts)


def _line_rows(report: BwaReport, line: BwaLine) -> str:
    """Die Zeile und – hinter ihr, zugeklappt – ihre Konten."""
    key = _esc(line.key)
    values = _numbers(cells(report, line, line.values))
    if line.kind != 'group':
        css = 'bwaSum bwaResult' if line.kind == 'result' else 'bwaSum'
        return f'<tr class="{css}"><td>{_esc(line.label)}</td>{values}</tr>'
    accounts = visible_accounts(report, line)
    if accounts:
        row = (f'<tr class="group-row" onclick="bwaToggle(\'{key}\')"><td>'
               f'<span class="split-toggle-icon" id="bwa-icon-{key}">▶</span> {_esc(line.label)}</td>{values}</tr>')
    else:
        row = (f'<tr><td><span class="split-toggle-icon bwaNoIcon">▶</span> {_esc(line.label)}</td>{values}</tr>')
    for account in accounts:
        label = f'{account.number} {account.name}' if account.name else account.number
        row += (f'<tr class="child-row" data-bwa-parent="{key}" hidden><td>{_esc(label)}</td>'
                f'{_numbers(cells(report, line, account.values))}</tr>')
    return row


def _table(report: BwaReport) -> str:
    first_block, second_block = block_headers(report)
    s = '<table class="bwaTable"><thead>'
    s += (f'<tr><th></th><th class="bwaBlock" colspan="4">{_esc(first_block)}</th>'
          f'<th class="bwaBlock" colspan="4">{_esc(second_block)}</th></tr>')
    s += '<tr><th>Position</th>' + ''.join(f'<th class="num">{_esc(label)}</th>' for label in column_labels(report)) + '</tr>'
    s += '</thead><tbody>'
    for line in report.lines:
        if visible(report, line):
            s += _line_rows(report, line)
    if visible(report, report.outside):
        s += f'<tr><th colspan="9">{_esc(OUTSIDE_HEADING)}</th></tr>' + _line_rows(report, report.outside)
    return s + '</tbody></table>'


def _form(query: BwaQuery) -> str:
    months = ''.join(f'<option value="{number}"{" selected" if number == query.month else ""}>{_esc(name)}</option>'
                     for number, name in enumerate(MONTH_NAMES, start=1))
    variants = ''.join(f'<option value="{_esc(key)}"{" selected" if key == query.variant else ""}>'
                       f'{_esc(VARIANT_LABELS[key])}</option>' for key in VARIANTS)
    return ('<form method="get" action="/bwa" class="rowWithObjects">'
            f'<label>Monat <select name="month">{months}</select></label>'
            f'<label>Jahr <input type="number" name="year" min="{MIN_YEAR}" max="{MAX_YEAR}" value="{_esc(query.year)}" required></label>'
            f'<label>Darstellung <select name="variant">{variants}</select></label>'
            '<button type="submit" class="coloredButton bg-blue btn-sm">Anzeigen</button></form>')


def PageBwa(db: Database, query: BwaQuery) -> str:
    """Die Seite zu einer geprüften Abfrage (``read_page_query``)."""
    report = db.get_bwa(query.year, query.month, query.variant)
    s = Header1('bwa')
    s += Header2('')
    s += Header3(_form(query))
    s += '<div class="rectRounded">'
    s += f'<h3>{_esc(TITLE)} – {_esc(month_label(query.year, query.month))}</h3>'
    s += f'<p class="hint">{_esc(HINT_TEXT)} {_esc(SIGN_TEXT)} {_esc(PERCENT_TEXT)}</p>'
    if query.invalid:
        s += f'<p class="warnColor">{_esc(INVALID_PERIOD_TEXT)}</p>'
    if visible(report, report.line('unassigned_income')) or visible(report, report.line('unassigned_expense')):
        s += f'<p class="hint">{_esc(UNASSIGNED_TEXT)}</p>'
    s += ('<div class="rowWithObjects">'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="bwaPdf()">PDF</button>'
          '<label><input type="checkbox" id="bwaAccounts" checked> mit Kontennachweis</label>'
          '<button type="button" class="coloredButton bg-gray btn-sm" onclick="bwaToggleAll()">Alle Konten auf-/zuklappen</button>'
          '</div>')
    s += f'<div class="splitScroll">{_table(report)}</div>'          # in einem schmalen Fenster rollt die Tabelle in sich
    s += '</div>'
    pdf_url = f'/bwa/pdf?year={query.year}&month={query.month}&variant={query.variant}'
    s += f'<script>\nvar BWA_DATA = {script_json({"pdf": pdf_url})};{BWA_JS}</script>'
    s += Footer()
    return s
