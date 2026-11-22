# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Summen- und Saldenliste (``GET /susa``, ``GET /susa/pdf``, ``GET /susa/csv``).

Die Seite zeigt je Konto den Anfangssaldo, Soll und Haben im Monat und aufgelaufen seit dem 1. Januar und den Endsaldo, nach Kontenklassen
gruppiert, darunter den Abgleich der Bankkonten mit den Bankbewegungen und die Hinweise zu den Buchungen. Gerechnet wird in ``db.susa`` und
``Database.get_susa`` (dieselben Zeilen und dieselbe Lesart wie der Export für das Steuerbüro), hier steht nur das HTML und das Lesen der Adresse des PDF.
Die Seite schreibt nichts in den Bestand: keine Einstellung, keine Datei, kein Steuerdatum (das Cookie des gemeinsamen Zeitraums setzt die Route
wie bei jeder Seite mit Zeitraum).

Jeder Wert aus Datenbank oder Adresse steht maskiert im HTML (``html.escape``); die Daten des Skripts gehen über ``script_json``. Die Seite nimmt
den gemeinsamen Zeitraum (``from``/``to`` in der Adresse, sonst das Cookie, sonst das laufende Jahr – ``period.resolve_period``) und leitet ihren
Monat daraus ab (``period.period_month``); ihre Menüzeile ist ``period_filter_widget`` mit Monatsknöpfen der Art ``always``. Das PDF und die CSV lesen
``year`` und ``month`` streng (``parse_year``, ``parse_month``): ein Schlüssel, der mehrfach vorkommt, ist ungültig.
"""
import html as _html
from dataclasses import dataclass

from db import Database
from db.bwa import month_label
from db.euer import german_date
from db.susa import (BANK_CHECK_TEXT, BANK_COLUMNS, BANK_DIFF_TEXT, BANK_HEADING, BANK_SKIPPED_TEXT, COLUMN_ACCOUNT, COLUMN_LABELS,
                     COLUMN_NAME, EUER_DIFF_TEXT, NOT_POSTED_TEXT, NOTICES_HEADING, OPENING_TEXT, SALDO_TEXT, SUM_LABEL, SUSA_HINT_TEXT,
                     TAX_TEXT, TITLE, TOTAL_GROUP, TRANSFER_TEXT, SusaAccount, SusaGroupTotal, SusaReport, block_headers, cells,
                     format_balance, notice_text, visible_accounts, visible_groups)
from .pages import Footer, Header1, Header2, Header3, script_json
from .pages_bwa import INVALID_PERIOD_ERROR, _single, parse_month, parse_year, report_period, reports_period_row, reports_submenu

SUSA_PDF_FAILED_TEXT = 'Die Summen- und Saldenliste ließ sich nicht als PDF erstellen.'
SUSA_CSV_FAILED_TEXT = 'Die Summen- und Saldenliste ließ sich nicht als CSV erstellen.'


@dataclass(frozen=True)
class SusaQuery:
    """Der Monat, den die Seite zeigt (sie leitet ihn vom gemeinsamen Zeitraum ab) und das PDF druckt (es liest Jahr und Monat aus der Adresse)."""
    year: int
    month: int


def read_susa_pdf_query(qs: dict) -> tuple[SusaQuery | None, str | None]:
    """Die Adresse des PDF lesen: ``(Abfrage, None)`` oder ``(None, Fehlertext)``. Jahr und Monat sind Pflicht."""
    _, year_text = _single(qs, 'year')
    _, month_text = _single(qs, 'month')
    year, month = parse_year(year_text), parse_month(month_text)
    if year is None or month is None:
        return None, INVALID_PERIOD_ERROR
    return SusaQuery(year, month), None


def _esc(value) -> str:
    """Wert aus Datenbank oder Adresse für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


# Der Download: Die Adresse steht nur im Skriptblock. Nur var und function.
SUSA_JS = '''
function susaPdf() { appDownload(SUSA_DATA.pdf); }
function susaCsv() { appDownload(SUSA_DATA.csv); }
'''


def _numbers(texts) -> str:
    return ''.join(f'<td class="num">{_esc(text)}</td>' for text in texts)


def _account_row(account: SusaAccount) -> str:
    """Eine Kontozeile; ein virtuelles Konto hat keine Nummer, und seine Bezeichnung steht gedämpft."""
    if account.virtual:
        label = f'<td></td><td class="hint">{_esc(account.name)}</td>'
    else:
        label = f'<td>{_esc(account.number)}</td><td>{_esc(account.name)}</td>'
    return f'<tr>{label}{_numbers(cells(account, False))}</tr>'


def _group_rows(group: SusaGroupTotal) -> str:
    s = f'<tr><th colspan="8">{_esc(group.group.label)}</th></tr>'
    s += ''.join(_account_row(account) for account in visible_accounts(group))
    s += (f'<tr class="bwaSum"><td colspan="2">{_esc(SUM_LABEL.format(gruppe=group.group.label))}</td>'
          f'{_numbers(cells(group, True))}</tr>')
    return s


def _table(report: SusaReport) -> str:
    first_block, second_block = block_headers(report.year, report.month)
    s = '<table class="reportTable"><thead>'
    s += (f'<tr><th colspan="3"></th><th class="bwaBlock" colspan="2">{_esc(first_block)}</th>'
          f'<th class="bwaBlock" colspan="2">{_esc(second_block)}</th><th></th></tr>')
    s += (f'<tr><th>{_esc(COLUMN_ACCOUNT)}</th><th>{_esc(COLUMN_NAME)}</th>'
          + ''.join(f'<th class="num">{_esc(label)}</th>' for label in COLUMN_LABELS) + '</tr>')
    s += '</thead><tbody>'
    s += ''.join(_group_rows(group) for group in visible_groups(report))
    s += (f'<tr class="bwaSum bwaResult"><td colspan="2">{_esc(TOTAL_GROUP.label)}</td>'
          f'{_numbers(cells(report.total, True))}</tr>')
    return s + '</tbody></table>'


def _bank_block(report: SusaReport) -> str:
    """Der Abgleich: je eigenem Bankkonto der Endsaldo der Liste gegen die Summe der Bankbewegungen – eine Abweichung steht in Warnfarbe."""
    until = german_date(report.periods['month'][1])
    s = f'<h4>{_esc(BANK_HEADING)}</h4>'
    s += f'<p class="hint">{_esc(BANK_CHECK_TEXT)}</p>'
    if report.bank_checks:
        heads = ''.join(('<th class="num">' if index > 1 else '<th>') + _esc(label.format(datum=until)) + '</th>'
                        for index, label in enumerate(BANK_COLUMNS))
        s += f'<div class="splitScroll"><table class="reportTable"><thead><tr>{heads}</tr></thead><tbody>'
        for check in report.bank_checks:
            warn = ' warnColor' if check.difference else ''
            s += (f'<tr><td>{_esc(check.number)}</td><td>{_esc(check.name)}</td>'
                  f'<td class="num">{_esc(format_balance(check.closing))}</td>'
                  f'<td class="num">{_esc(format_balance(check.movements))}</td>'
                  f'<td class="num{warn}">{_esc(format_balance(check.difference))}</td></tr>')
        s += '</tbody></table></div>'
    if any(check.difference for check in report.bank_checks):
        s += f'<p class="hint">{_esc(BANK_DIFF_TEXT)}</p>'
    if report.bank_skipped:
        s += f'<p class="hint">{_esc(BANK_SKIPPED_TEXT.format(names=", ".join(report.bank_skipped)))}</p>'
    return s


def _notices_block(report: SusaReport) -> str:
    if not report.notices:
        return ''
    until = german_date(report.periods['month'][1])
    return (f'<h4>{_esc(NOTICES_HEADING.format(datum=until))}</h4><ul>'
            + ''.join(f'<li>{_esc(notice_text(notice))}</li>' for notice in report.notices) + '</ul>')


def PageSusa(db: Database, query: SusaQuery, date_from: str | None = None, date_to: str | None = None, today=None) -> str:
    """Die Seite zu einem Monat (``query``); ``date_from``/``date_to`` ist der gemeinsame Zeitraum der Menüzeile (ohne ihn der Monat der
    Abfrage), ``today`` bestimmt dort das laufende Jahr."""
    date_from, date_to = report_period(query.year, query.month, date_from, date_to)
    report = db.get_susa(query.year, query.month)
    s = Header1('bwa')
    s += Header2(reports_submenu('susa'))
    s += Header3(reports_period_row('/susa', date_from, date_to, today))
    s += '<div class="rectRounded">'
    s += f'<h3>{_esc(TITLE)} – {_esc(month_label(query.year, query.month))}</h3>'
    s += f'<p class="hint">{_esc(SUSA_HINT_TEXT)} {_esc(SALDO_TEXT)}</p>'
    s += ('<div class="rowWithObjects">'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="susaPdf()">PDF</button>'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="susaCsv()">CSV</button>'
          '</div>')
    s += f'<div class="splitScroll">{_table(report)}</div>'          # in einem schmalen Fenster rollt die Tabelle in sich
    s += _bank_block(report)
    s += _notices_block(report)
    for text in (OPENING_TEXT, NOT_POSTED_TEXT, TRANSFER_TEXT, TAX_TEXT, EUER_DIFF_TEXT):
        s += f'<p class="hint">{_esc(text)}</p>'
    s += '</div>'
    pdf_url = f'/susa/pdf?year={query.year}&month={query.month}'
    csv_url = f'/susa/csv?year={query.year}&month={query.month}'
    s += f'<script>\nvar SUSA_DATA = {script_json({"pdf": pdf_url, "csv": csv_url})};{SUSA_JS}</script>'
    s += Footer()
    return s
