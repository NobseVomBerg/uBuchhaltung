# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Umsatzsteuer-Voranmeldung (``GET /ustva``, ``GET /ustva/pdf``).

Die Seite zeigt die Kennzahlen 81, 86, 66 und 83 eines Monats oder Quartals – zum Übertragen in den Vordruck, die App übermittelt nichts –, darunter die
Abstimmung mit der EÜR und alles, was die Seite keiner Kennzahl sicher zuordnet („Nicht zugeordnet“); unter den Zeilen mit Konten lassen sich diese
aufklappen. Gerechnet wird in ``db.ustva`` und ``Database.get_ustva`` (dieselben Sätze wie die EÜR), hier steht nur das HTML. Die Seite schreibt nichts
in den Bestand: keine Einstellung, keine Datei (das Cookie des gemeinsamen Zeitraums setzt die Route wie bei jeder Seite mit Zeitraum). Zahlen zeigt sie nur
bei Ist-Versteuerung (``Database.get_vat_method``); sonst steht ein fester Satz da und ``get_ustva`` läuft nicht.

Die Seite nimmt den gemeinsamen Zeitraum (``from``/``to`` in der Adresse, sonst das Cookie, sonst das laufende Jahr – ``period.resolve_period``) und leitet
ihren Monat daraus ab (``period.period_month``); ihre Menüzeile ist ``period_filter_widget`` mit Monatsknöpfen der Art ``always``. Nur der Anmeldezeitraum
(``art``: ``monat`` oder ``quartal``) kommt aus der Adresse (``read_kind``) und steht als Auswahl im Inhalt; bei einem Quartal gilt das Quartal dieses Monats.
Das PDF liest ``year``, ``month`` und ``art`` streng (``read_ustva_pdf_query``): ein Schlüssel, der mehrfach vorkommt, ist ungültig, ein Quartal braucht
den Monat 3, 6, 9 oder 12.

Jeder Wert aus Datenbank oder Adresse steht maskiert im HTML (``html.escape``); die Daten des Skripts gehen über ``script_json``.
"""
import datetime
import html as _html
from dataclasses import dataclass

from db import Database
from db.bwa import format_amount
from db.euer import german_date
from db.ustva import (COLUMN_BASE, COLUMN_KZ, COLUMN_LABEL, COLUMN_TAX, COLUMN_WHOLE, KIND_LABELS, KIND_MONTH, KIND_QUARTER, LABEL_83, LABEL_VAT_TOTAL,
                      RECON_COLUMNS, RECON_HEADING, UNASSIGNED_COLUMNS, UNASSIGNED_HEADING, UNASSIGNED_NONE_TEXT, UNASSIGNED_SUM_LABEL, UNBALANCED_LABEL,
                      UNBOOKED_LABEL, USTVA_DATE_TEXT, USTVA_HINT_TEXT, USTVA_KIND_LABEL, USTVA_KINDS, USTVA_NOT_SHOWN_TEXT, USTVA_OPEN_PERIOD_TEXT,
                      USTVA_RECON_TEXT, USTVA_ROUNDING_TEXT, USTVA_SIGN_TEXT, USTVA_SMALL_BUSINESS_TEXT, USTVA_SOLL_TEXT, USTVA_TITLE, USTVA_UNASSIGNED_TEXT,
                      USTVA_UNSET_TEXT, Figure, UstvaReport, account_label, format_recon, format_whole, is_calendar_quarter, period_label, ustva_period)
from .pages import Footer, Header1, Header2, Header3, script_json
from .pages_bwa import _single, parse_month, parse_year, reports_submenu
from .period import MONTHS_ALWAYS, period_filter_widget

INVALID_PERIOD_ERROR = 'Ungültiger Zeitraum.'
USTVA_PDF_FAILED_TEXT = 'Die Umsatzsteuer-Voranmeldung ließ sich nicht als PDF erstellen.'


@dataclass(frozen=True)
class UstvaQuery:
    """Was angezeigt wird: Jahr, Monat und Anmeldezeitraum (bei ``quartal`` ist der Monat immer 3, 6, 9 oder 12 – der letzte des Quartals)."""
    year: int
    month: int
    kind: str


def read_kind(qs: dict, date_from: str, date_to: str) -> str:
    """Der Anmeldezeitraum, den die Seite zeigt: ``art`` genau einmal in der Adresse und ``monat`` oder ``quartal`` – dieser Wert; sonst ``quartal``, wenn der
    gemeinsame Zeitraum genau ein Kalenderquartal ist, und sonst ``monat``. Ohne Hinweis."""
    _, text = _single(qs, 'art')
    if isinstance(text, str) and text in USTVA_KINDS:
        return text
    return KIND_QUARTER if is_calendar_quarter(date_from, date_to) else KIND_MONTH


def read_ustva_pdf_query(qs: dict) -> tuple[UstvaQuery | None, str | None]:
    """Die Adresse des PDF lesen: ``(Abfrage, None)`` oder ``(None, Fehlertext)``. Jahr und Monat sind Pflicht; ``art`` ist ``monat``, wenn sie fehlt, und
    ein Fehler, wenn sie mehrfach vorkommt oder unbekannt ist. Ein Quartal braucht den Monat 3, 6, 9 oder 12 – nichts wird getauscht oder korrigiert."""
    _, year_text = _single(qs, 'year')
    _, month_text = _single(qs, 'month')
    year, month = parse_year(year_text), parse_month(month_text)
    if year is None or month is None:
        return None, INVALID_PERIOD_ERROR
    kind_given, kind_text = _single(qs, 'art')
    kind = KIND_MONTH
    if kind_given:
        if kind_text not in USTVA_KINDS:
            return None, INVALID_PERIOD_ERROR
        kind = kind_text
    if kind == KIND_QUARTER and month not in (3, 6, 9, 12):
        return None, INVALID_PERIOD_ERROR
    return UstvaQuery(year, month, kind), None


def _esc(value) -> str:
    """Wert aus Datenbank oder Adresse für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


def _kind_form(date_from: str, date_to: str, kind: str) -> str:
    """Die Auswahl „Anmeldezeitraum“ als kleines GET-Formular für die Zeile mit dem PDF-Knopf im Inhalt – nicht in der Menüzeile (Auswahlfelder sind höher,
    die Querlinie unter den Menüzeilen spränge); wie ``pages_bwa.variant_form``. Der Zeitraum geht in versteckten Feldern mit, ein Wechsel lädt sofort neu."""
    options = ''.join(f'<option value="{_esc(key)}"{" selected" if key == kind else ""}>{_esc(KIND_LABELS[key])}</option>' for key in USTVA_KINDS)
    return (f'<form method="get" action="/ustva" class="rowWithObjects">'
            f'<input type="hidden" name="from" value="{_esc(date_from)}"><input type="hidden" name="to" value="{_esc(date_to)}">'
            f'<label>{_esc(USTVA_KIND_LABEL)} <select name="art" onchange="this.form.submit()">{options}</select></label></form>')


# Aufklappen der Konten unter einer Zeile: Die Konten sind Zeilen mit data-ustva-parent, das Symbol hat die ID ustva-icon-<Schlüssel>.
# Nur var und function, Werte nur über textContent und Eigenschaften.
USTVA_JS = '''
function ustvaPdf() { appDownload(USTVA_DATA.pdf); }
function ustvaSetGroup(key, open) {
  var rows = document.querySelectorAll('tr[data-ustva-parent="' + key + '"]');
  for (var i = 0; i < rows.length; i++) { rows[i].hidden = !open; }
  var icon = document.getElementById('ustva-icon-' + key);
  if (icon) { icon.textContent = open ? '\\u25BC' : '\\u25B6'; }
}
function ustvaToggle(key) {
  var first = document.querySelector('tr[data-ustva-parent="' + key + '"]');
  ustvaSetGroup(key, !!first && first.hidden);
}
function ustvaToggleAll() {
  var icons = document.querySelectorAll('.split-toggle-icon[id^="ustva-icon-"]');
  var open = false;
  for (var i = 0; i < icons.length; i++) {
    var first = document.querySelector('tr[data-ustva-parent="' + icons[i].id.substring(11) + '"]');
    if (first && first.hidden) { open = true; }
  }
  for (var j = 0; j < icons.length; j++) { ustvaSetGroup(icons[j].id.substring(11), open); }
}
'''


def _num(text) -> str:
    return f'<td class="num">{_esc(text)}</td>'


def _group_row(key: str, first_cells: str, label: str, values: str, has_children: bool) -> str:
    """Die Zeile mit dem Pfeil (``ustva-icon-<Schlüssel>``, klappt die Kontenzeilen auf); ohne Konten ein Pfeil, der nicht zu sehen ist (``bwaNoIcon``)."""
    if has_children:
        return (f'<tr class="group-row" onclick="ustvaToggle(\'{key}\')">{first_cells}'
                f'<td><span class="split-toggle-icon" id="ustva-icon-{key}">▶</span> {_esc(label)}</td>{values}</tr>')
    return f'<tr>{first_cells}<td><span class="split-toggle-icon bwaNoIcon">▶</span> {_esc(label)}</td>{values}</tr>'


def _figure_rows(figure: Figure, with_base: bool) -> str:
    """Eine Kennzahl und – hinter ihr, zugeklappt – ihre Konten. ``with_base`` false (Kz 66): nur die Steuer."""
    key = _esc(figure.key)
    values = (_num(format_amount(figure.base)) + _num(format_whole(figure.base)) if with_base else _num('') + _num('')) + _num(format_amount(figure.tax))
    row = _group_row(key, f'<td>{_esc(figure.kz)}</td>', figure.label, values, bool(figure.accounts))
    for line in figure.accounts:
        base = _num(format_amount(line.base)) if with_base else _num('')
        row += (f'<tr class="child-row" data-ustva-parent="{key}" hidden><td></td><td>{_esc(account_label(line))}</td>'
                f'{base}{_num("")}{_num(format_amount(line.tax))}</tr>')
    return row


def _figures_table(report: UstvaReport) -> str:
    s = ('<table class="bwaTable"><thead><tr>'
         f'<th>{_esc(COLUMN_KZ)}</th><th>{_esc(COLUMN_LABEL)}</th><th class="num">{_esc(COLUMN_BASE)}</th>'
         f'<th class="num">{_esc(COLUMN_WHOLE)}</th><th class="num">{_esc(COLUMN_TAX)}</th></tr></thead><tbody>')
    for figure in report.figures:
        s += _figure_rows(figure, True)
    s += f'<tr class="bwaSum"><td></td><td>{_esc(LABEL_VAT_TOTAL)}</td>{_num("")}{_num("")}{_num(format_amount(report.vat_total))}</tr>'
    s += _figure_rows(report.input_tax, False)
    s += f'<tr class="bwaSum bwaResult"><td>83</td><td>{_esc(LABEL_83)}</td>{_num("")}{_num("")}{_num(format_amount(report.prepayment))}</tr>'
    return s + '</tbody></table>'


def _reconciliation_table(report: UstvaReport) -> str:
    """Die Abstimmung: die Steuer der Kennzahlen gegen Umsatzsteuer abzüglich Vorsteuer laut EÜR, mit der Abweichung – eine Wertezeile (ein Wert, den die EÜR
    nicht als ganze Zahl liefert, steht als „–“ da)."""
    heads = ''.join(f'<th class="num">{_esc(label)}</th>' for label in RECON_COLUMNS)
    values = ''.join(_num(format_recon(value)) for value in (report.recon_page, report.recon_euer, report.recon_difference))
    return (f'<table class="reportTable"><thead><tr><th colspan="3">{_esc(RECON_HEADING)}</th></tr><tr>{heads}</tr></thead>'
            f'<tbody><tr>{values}</tr></tbody></table>')


def _unassigned_table(report: UstvaReport) -> str:
    """„Nicht zugeordnet“: je Grund Anzahl, Netto und Steuer mit den Konten dahinter; Bankbewegungen ohne Satz und Bankbewegungen, deren Sätze nicht ihren Betrag
    ergeben, stehen als eigene Zeilen, zählen aber nicht in der Summe (sie sind kein Satz)."""
    s = f'<table class="bwaTable"><thead><tr><th colspan="4">{_esc(UNASSIGNED_HEADING)}</th></tr><tr><th>{_esc(UNASSIGNED_COLUMNS[0])}</th>'
    s += ''.join(f'<th class="num">{_esc(label)}</th>' for label in UNASSIGNED_COLUMNS[1:]) + '</tr></thead><tbody>'
    for group in report.unassigned:
        key = _esc(f'n-{group.key}')
        values = _num(group.count) + _num(format_amount(group.net)) + _num(format_amount(group.tax))
        s += _group_row(key, '', group.label, values, bool(group.accounts))
        for line in group.accounts:
            s += (f'<tr class="child-row" data-ustva-parent="{key}" hidden><td>{_esc(account_label(line))}</td>'
                  f'{_num(line.count)}{_num(format_amount(line.base))}{_num(format_amount(line.tax))}</tr>')
    if report.unbooked_count:
        s += f'<tr><td>{_esc(UNBOOKED_LABEL)}</td>{_num(report.unbooked_count)}{_num(format_amount(report.unbooked_amount))}{_num("")}</tr>'
    if report.unbalanced_count:
        s += f'<tr><td>{_esc(UNBALANCED_LABEL)}</td>{_num(report.unbalanced_count)}{_num(format_amount(report.unbalanced_amount))}{_num("")}</tr>'
    if not report.unassigned and not report.unbooked_count and not report.unbalanced_count:
        s += f'<tr><td colspan="4">{_esc(UNASSIGNED_NONE_TEXT)}</td></tr>'
    s += (f'<tr class="bwaSum"><td>{_esc(UNASSIGNED_SUM_LABEL)}</td>{_num("")}'
          f'{_num(format_amount(report.unassigned_net))}{_num(format_amount(report.unassigned_tax))}</tr>')
    return s + '</tbody></table>'


def PageUstva(db: Database, query: UstvaQuery, date_from: str | None = None, date_to: str | None = None, today: datetime.date | None = None) -> str:
    """Die Seite zu einem Monat oder Quartal (``query``); ``date_from``/``date_to`` ist der gemeinsame Zeitraum der Menüzeile (ohne ihn der Zeitraum der
    Abfrage), ``today`` bestimmt dort das laufende Jahr und ob der Zeitraum noch läuft. Nur bei Ist-Versteuerung stehen Zahlen da."""
    period_from, period_to = ustva_period(query.year, query.month, query.kind)
    if date_from is None or date_to is None:
        date_from, date_to = period_from, period_to
    s = Header1('bwa')
    s += Header2(reports_submenu('ustva'))
    s += Header3(period_filter_widget(date_from, date_to, '/ustva', extra_params={'art': None if query.kind == KIND_MONTH else query.kind},
                                      months=MONTHS_ALWAYS, today=today))
    s += '<div class="rectRounded">'
    s += (f'<h3>{_esc(USTVA_TITLE)} – {_esc(period_label(query.year, query.month, query.kind))} '
          f'({_esc(german_date(period_from))} – {_esc(german_date(period_to))})</h3>')
    method = db.get_vat_method()
    if method != 'ist':
        s += f'<p class="hint warnColor">{_esc(USTVA_SOLL_TEXT if method == "soll" else USTVA_UNSET_TEXT)}</p>'
        s += f'<p class="hint">{_esc(USTVA_SMALL_BUSINESS_TEXT)}</p>'
        return s + '</div>' + Footer()
    report = db.get_ustva(query.year, query.month, query.kind)
    s += f'<p class="hint">{_esc(USTVA_HINT_TEXT)} {_esc(USTVA_DATE_TEXT)} {_esc(USTVA_ROUNDING_TEXT)}</p>'
    if report.date_to > (today or datetime.date.today()).isoformat():
        s += f'<p class="hint warnColor">{_esc(USTVA_OPEN_PERIOD_TEXT)}</p>'
    if report.unassigned or report.unbooked_count or report.unbalanced_count:
        s += f'<p class="hint warnColor">{_esc(USTVA_UNASSIGNED_TEXT)}</p>'
    if report.has_small_business:
        s += f'<p class="hint">{_esc(USTVA_SMALL_BUSINESS_TEXT)}</p>'
    s += ('<div class="rowWithObjects">'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="ustvaPdf()">PDF</button>'
          '<button type="button" class="coloredButton bg-gray btn-sm" onclick="ustvaToggleAll()">Alle Konten auf-/zuklappen</button>'
          + _kind_form(date_from, date_to, query.kind) +
          '</div>')
    s += f'<div class="splitScroll">{_figures_table(report)}</div>'          # in einem schmalen Fenster rollt die Tabelle in sich
    s += f'<div class="splitScroll">{_reconciliation_table(report)}</div>'
    s += f'<p class="hint">{_esc(USTVA_RECON_TEXT)}</p>'
    s += f'<div class="splitScroll">{_unassigned_table(report)}</div>'
    s += f'<p class="hint">{_esc(USTVA_NOT_SHOWN_TEXT)} {_esc(USTVA_SIGN_TEXT)}</p>'
    s += '</div>'
    pdf_url = f'/ustva/pdf?year={query.year}&month={query.month}&art={query.kind}'
    s += f'<script>\nvar USTVA_DATA = {script_json({"pdf": pdf_url})};{USTVA_JS}</script>'
    s += Footer()
    return s
