# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Liquiditätsrechnung (``GET /liquiditaet``, ``GET /liquiditaet/pdf``).

Die Seite zeigt, wie aus dem Ergebnis eines Monats – und aufgelaufen der Zeit seit dem 1. Januar – die Veränderung der flüssigen Mittel wird:
Abschreibungen und Restbuchwerte zurück, Darlehen und Verbindlichkeiten, Anlagenzugänge und Privatentnahmen dazu, am Ende die Veränderung laut
Kontobewegungen und der Rest, den die Überleitung nicht erklärt („Nicht zugeordnet“). Wahlweise im Vergleich mit dem Vorjahr oder nur Monat und Jahr;
unter den Zeilen mit Konten lassen sich diese aufklappen. Gerechnet wird in ``db.liquidity`` und ``Database.get_liquidity`` (aus den Zeilen der EÜR,
dem Bericht der betriebswirtschaftlichen Auswertung und den Töpfen des Dashboards), hier steht nur das Lesen der Adresse und das HTML. Die Seite schreibt
nichts: kein Cookie, keine Einstellung, keine Datei.

Die Adresse (``year``, ``month``, ``variant``) liest dieselbe strenge Regel wie die betriebswirtschaftliche Auswertung (``pages_bwa.read_page_query``,
``read_pdf_query``); ein Schlüssel, der mehrfach vorkommt, ist ungültig. Jeder Wert aus Datenbank oder Adresse steht maskiert im HTML (``html.escape``);
die Daten des Skripts gehen über ``script_json``.
"""
import html as _html

from db import Database
from db.bwa import INVALID_PERIOD_TEXT, MONTH_NAMES, VARIANTS, block_headers, month_label
from db.liquidity import (BALANCED_TEXT, HINT_TEXT, MEMO, METHOD_TEXT, RESULT, ROUNDING_TEXT, SIGN_TEXT, SUM, TITLE, TRANSFER_TEXT, UNASSIGNED_TEXT,
                          UNBALANCED_TEXT, VARIANT_LABELS, LiquidityLine, LiquidityReport, account_label, balanced, block_size, cells, column_labels,
                          line_label, rounding_remainder, visible, visible_accounts)
from .pages import Footer, Header1, Header2, Header3, script_json
from .pages_bwa import MAX_YEAR, MIN_YEAR, BwaQuery, reports_submenu

LIQUIDITY_PDF_FAILED_TEXT = 'Die Liquiditätsrechnung ließ sich nicht als PDF erstellen.'


def _esc(value) -> str:
    """Wert aus Datenbank oder Adresse für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


# Aufklappen der Konten unter einer Zeile: Die Konten sind Zeilen mit data-liq-parent, das Symbol hat die ID liq-icon-<Schlüssel>.
# Nur var und function, Werte nur über textContent und Eigenschaften.
LIQUIDITY_JS = '''
function liqPdf() {
  var box = document.getElementById('liqAccounts');
  appDownload(LIQUIDITY_DATA.pdf + '&accounts=' + (box && box.checked ? '1' : '0'));
}
function liqSetGroup(key, open) {
  var rows = document.querySelectorAll('tr[data-liq-parent="' + key + '"]');
  for (var i = 0; i < rows.length; i++) { rows[i].hidden = !open; }
  var icon = document.getElementById('liq-icon-' + key);
  if (icon) { icon.textContent = open ? '\\u25BC' : '\\u25B6'; }
}
function liqToggle(key) {
  var first = document.querySelector('tr[data-liq-parent="' + key + '"]');
  liqSetGroup(key, !!first && first.hidden);
}
function liqToggleAll() {
  var icons = document.querySelectorAll('.split-toggle-icon[id^="liq-icon-"]');
  var open = false;
  for (var i = 0; i < icons.length; i++) {
    var first = document.querySelector('tr[data-liq-parent="' + icons[i].id.substring(9) + '"]');
    if (first && first.hidden) { open = true; }
  }
  for (var j = 0; j < icons.length; j++) { liqSetGroup(icons[j].id.substring(9), open); }
}
'''


def _numbers(texts) -> str:
    return ''.join(f'<td class="num">{_esc(text)}</td>' for text in texts)


def _line_rows(report: LiquidityReport, line: LiquidityLine) -> str:
    """Die Zeile und – hinter ihr, zugeklappt – ihre Konten."""
    key = _esc(line.key)
    label = _esc(line_label(report, line))
    values = _numbers(cells(report, line.values))
    if line.kind in (SUM, RESULT):
        css = 'bwaSum bwaResult' if line.kind == RESULT else 'bwaSum'
        return f'<tr class="{css}"><td>{label}</td>{values}</tr>'
    if line.kind == MEMO:
        return f'<tr class="hint"><td>{label}</td>{values}</tr>'
    accounts = visible_accounts(report, line)
    warn = ' class="warnColor"' if line.key == 'unassigned' and any(line.values.values()) else ''
    if accounts:
        row = (f'<tr class="group-row" onclick="liqToggle(\'{key}\')"><td>'
               f'<span class="split-toggle-icon" id="liq-icon-{key}">▶</span> {label}</td>{values}</tr>')
    else:
        row = f'<tr{warn}><td><span class="split-toggle-icon bwaNoIcon">▶</span> {label}</td>{values}</tr>'
    for account in accounts:
        row += (f'<tr class="child-row" data-liq-parent="{key}" hidden><td>{_esc(account_label(account))}</td>'
                f'{_numbers(cells(report, account.values))}</tr>')
    return row


def _table(report: LiquidityReport) -> str:
    first_block, second_block = block_headers(report)
    size = block_size(report)
    s = '<table class="bwaTable"><thead>'
    s += (f'<tr><th></th><th class="bwaBlock" colspan="{size}">{_esc(first_block)}</th>'
          f'<th class="bwaBlock" colspan="{size}">{_esc(second_block)}</th></tr>')
    s += '<tr><th>Position</th>' + ''.join(f'<th class="num">{_esc(label)}</th>' for label in column_labels(report)) + '</tr>'
    s += '</thead><tbody>'
    for line in report.lines:
        if visible(report, line):
            s += _line_rows(report, line)
    return s + '</tbody></table>'


def _form(query: BwaQuery) -> str:
    months = ''.join(f'<option value="{number}"{" selected" if number == query.month else ""}>{_esc(name)}</option>'
                     for number, name in enumerate(MONTH_NAMES, start=1))
    variants = ''.join(f'<option value="{_esc(key)}"{" selected" if key == query.variant else ""}>'
                       f'{_esc(VARIANT_LABELS[key])}</option>' for key in VARIANTS)
    return ('<form method="get" action="/liquiditaet" class="rowWithObjects">'
            f'<label>Monat <select name="month">{months}</select></label>'
            f'<label>Jahr <input type="number" name="year" min="{MIN_YEAR}" max="{MAX_YEAR}" value="{_esc(query.year)}" required></label>'
            f'<label>Darstellung <select name="variant">{variants}</select></label>'
            '<button type="submit" class="coloredButton bg-blue btn-sm">Anzeigen</button></form>')


def _reconciliation(report: LiquidityReport) -> str:
    """Der Satz zur Abstimmung unter der Tabelle: Die Überleitung geht auf – oder die Warnung, was „Nicht zugeordnet“ ist (und, wenn nötig, dass ein Rest
    unter einem halben Cent ein Rundungsrest ist). Dazu, wenn Überträge dastehen, der Satz, warum sie nicht enthalten sind."""
    s = ''
    if balanced(report):
        s += f'<p class="hint">{_esc(BALANCED_TEXT)}</p>'
    else:
        s += f'<p class="warnColor">{_esc(UNBALANCED_TEXT)}</p><p class="hint">{_esc(UNASSIGNED_TEXT)}</p>'
        if rounding_remainder(report):
            s += f'<p class="hint">{_esc(ROUNDING_TEXT)}</p>'
    if visible(report, report.line('memo_transfers')):
        s += f'<p class="hint">{_esc(TRANSFER_TEXT)}</p>'
    return s


def PageLiquidity(db: Database, query: BwaQuery) -> str:
    """Die Seite zu einer geprüften Abfrage (``pages_bwa.read_page_query``)."""
    report = db.get_liquidity(query.year, query.month, query.variant)
    s = Header1('bwa')
    s += Header2(reports_submenu('liquiditaet'))
    s += Header3(_form(query))
    s += '<div class="rectRounded">'
    s += f'<h3>{_esc(TITLE)} – {_esc(month_label(query.year, query.month))}</h3>'
    s += f'<p class="hint">{_esc(HINT_TEXT)} {_esc(SIGN_TEXT)}</p>'
    s += f'<p class="hint">{_esc(METHOD_TEXT)}</p>'
    if query.invalid:
        s += f'<p class="warnColor">{_esc(INVALID_PERIOD_TEXT)}</p>'
    s += ('<div class="rowWithObjects">'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="liqPdf()">PDF</button>'
          '<label><input type="checkbox" id="liqAccounts" checked> mit Kontennachweis</label>'
          '<button type="button" class="coloredButton bg-gray btn-sm" onclick="liqToggleAll()">Alle Konten auf-/zuklappen</button>'
          '</div>')
    s += f'<div class="splitScroll">{_table(report)}</div>'          # in einem schmalen Fenster rollt die Tabelle in sich
    s += _reconciliation(report)
    s += '</div>'
    pdf_url = f'/liquiditaet/pdf?year={query.year}&month={query.month}&variant={query.variant}'
    s += f'<script>\nvar LIQUIDITY_DATA = {script_json({"pdf": pdf_url})};{LIQUIDITY_JS}</script>'
    s += Footer()
    return s
