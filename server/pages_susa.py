# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Summen- und Saldenliste (``GET /susa``, ``GET /susa/pdf``).

Die Seite zeigt je Konto den Anfangssaldo, Soll und Haben im Monat und aufgelaufen seit dem 1. Januar und den Endsaldo, nach Kontenklassen
gruppiert, darunter den Abgleich der Bankkonten mit den Bankbewegungen und die Hinweise zu den Buchungen. Gerechnet wird in ``db.susa`` und
``Database.get_susa`` (dieselben Zeilen und dieselbe Lesart wie der Export für das Steuerbüro), hier steht nur das Lesen der Adresse und das HTML.
Die Seite schreibt nichts: kein Cookie, keine Einstellung, keine Datei, kein Steuerdatum.

Jeder Wert aus Datenbank oder Adresse steht maskiert im HTML (``html.escape``); die Daten des Skripts gehen über ``script_json``. Gelesen wird
die Adresse streng (``parse_year``, ``parse_month``): ein Schlüssel, der mehrfach vorkommt, ist ungültig.
"""
import html as _html
from dataclasses import dataclass

from db import Database
from db.bwa import INVALID_PERIOD_TEXT, MONTH_NAMES, default_month, month_label
from db.euer import german_date
from db.susa import (BANK_CHECK_TEXT, BANK_COLUMNS, BANK_DIFF_TEXT, BANK_HEADING, BANK_SKIPPED_TEXT, COLUMN_ACCOUNT, COLUMN_LABELS,
                     COLUMN_NAME, EUER_DIFF_TEXT, NOT_POSTED_TEXT, NOTICES_HEADING, OPENING_TEXT, SALDO_TEXT, SUM_LABEL, SUSA_HINT_TEXT,
                     TAX_TEXT, TITLE, TOTAL_GROUP, TRANSFER_TEXT, SusaAccount, SusaGroupTotal, SusaReport, block_headers, cells,
                     format_balance, notice_text, visible_accounts, visible_groups)
from .pages import Footer, Header1, Header2, Header3, script_json
from .pages_bwa import INVALID_PERIOD_ERROR, MAX_YEAR, MIN_YEAR, _single, parse_month, parse_year, reports_submenu

SUSA_PDF_FAILED_TEXT = 'Die Summen- und Saldenliste ließ sich nicht als PDF erstellen.'


@dataclass(frozen=True)
class SusaQuery:
    """Was die Adresse verlangt: Jahr und Monat; ``invalid``: Der angegebene Zeitraum ließ sich nicht lesen, es gilt der Vormonat (die Seite
    sagt es)."""
    year: int
    month: int
    invalid: bool = False


def read_susa_page_query(qs: dict, today) -> SusaQuery:
    """Die Adresse der Seite lesen. Ohne Jahr und Monat gilt der Vormonat; ist einer von beiden ungültig oder fehlt nur einer, ebenfalls –
    dann mit ``invalid`` (Hinweis auf der Seite)."""
    year_given, year_text = _single(qs, 'year')
    month_given, month_text = _single(qs, 'month')
    year, month = parse_year(year_text), parse_month(month_text)
    if year is not None and month is not None:
        return SusaQuery(year, month)
    default_year, default_month_number = default_month(today)
    return SusaQuery(default_year, default_month_number, invalid=year_given or month_given)


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


def _form(query: SusaQuery) -> str:
    months = ''.join(f'<option value="{number}"{" selected" if number == query.month else ""}>{_esc(name)}</option>'
                     for number, name in enumerate(MONTH_NAMES, start=1))
    return ('<form method="get" action="/susa" class="rowWithObjects">'
            f'<label>Monat <select name="month">{months}</select></label>'
            f'<label>Jahr <input type="number" name="year" min="{MIN_YEAR}" max="{MAX_YEAR}" value="{_esc(query.year)}" required></label>'
            '<button type="submit" class="coloredButton bg-blue btn-sm">Anzeigen</button></form>')


def PageSusa(db: Database, query: SusaQuery) -> str:
    """Die Seite zu einer geprüften Abfrage (``read_susa_page_query``)."""
    report = db.get_susa(query.year, query.month)
    s = Header1('bwa')
    s += Header2(reports_submenu('susa'))
    s += Header3(_form(query))
    s += '<div class="rectRounded">'
    s += f'<h3>{_esc(TITLE)} – {_esc(month_label(query.year, query.month))}</h3>'
    s += f'<p class="hint">{_esc(SUSA_HINT_TEXT)} {_esc(SALDO_TEXT)}</p>'
    if query.invalid:
        s += f'<p class="warnColor">{_esc(INVALID_PERIOD_TEXT)}</p>'
    s += ('<div class="rowWithObjects">'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="susaPdf()">PDF</button>'
          '</div>')
    s += f'<div class="splitScroll">{_table(report)}</div>'          # in einem schmalen Fenster rollt die Tabelle in sich
    s += _bank_block(report)
    s += _notices_block(report)
    for text in (OPENING_TEXT, NOT_POSTED_TEXT, TRANSFER_TEXT, TAX_TEXT, EUER_DIFF_TEXT):
        s += f'<p class="hint">{_esc(text)}</p>'
    s += '</div>'
    pdf_url = f'/susa/pdf?year={query.year}&month={query.month}'
    s += f'<script>\nvar SUSA_DATA = {script_json({"pdf": pdf_url})};{SUSA_JS}</script>'
    s += Footer()
    return s
