# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Einnahmenüberschussrechnung als eigene Seite (``GET /euer``, ``GET /euer/pdf``).

Die Seite zeigt die EÜR eines frei gewählten Zeitraums, aufbereitet von ``db.euer`` – derselben Aufbereitung wie der Kasten des
Dashboards, nur ohne Kontofilter: Mit Filter fielen das Verrechnungskonto (1460) und die Abschreibungen weg, und es wäre keine EÜR
mehr. Gerechnet wird in ``db.euer``, hier stehen nur das HTML und das Lesen der Adresse des PDF. Die Seite schreibt nichts in den
Bestand: keine Einstellung, keine Datei (das Cookie des gemeinsamen Zeitraums setzt die Route wie bei jeder Seite mit Zeitraum).

Jeder Wert aus Datenbank oder Adresse steht maskiert im HTML (``html.escape``); die Daten des Skripts gehen über ``script_json``.
Die Seite nimmt den gemeinsamen Zeitraum (``from``/``to`` in der Adresse, sonst das Cookie, sonst das laufende Jahr –
``period.resolve_period``, ein ungültiger Wert wird wie überall verworfen, „von“ nach „bis“ getauscht); ihre Menüzeile ist
``period_filter_widget``. Das PDF liest die Adresse streng (``period.parse_iso_date``): ``from`` und ``to`` nur beide und als
ISO-Datum, ``from`` nicht nach ``to`` (kein stiller Tausch), ein Schlüssel, der mehrfach vorkommt, ist ungültig.
"""
import datetime
import html as _html
from dataclasses import dataclass
from urllib.parse import quote

from db import Database
from db.bwa import format_amount
from db.euer import (COLUMN_ACCOUNT, COLUMN_AMOUNT, COLUMN_NAME, EUER_HINT_TEXT, EUER_OTHER_TEXT, EUER_TITLE, EXPENSE_HEADING,
                     EXPENSE_SUM_LABEL, INCOME_HEADING, INCOME_SUM_LABEL, OTHER_HEADING, OTHER_SUM_LABEL, RESULT_LABEL, EuerLine,
                     EuerStatement, euer_statement, range_label)
from db.reporting import bank_skr_numbers
from .pages import Footer, Header1, Header2, Header3, script_json
from .pages_bwa import _single, reports_submenu
from .period import parse_iso_date, period_filter_widget

INVALID_PERIOD_ERROR = 'Ungültiger Zeitraum.'
EUER_PDF_FAILED_TEXT = 'Die Einnahmenüberschussrechnung ließ sich nicht als PDF erstellen.'


@dataclass(frozen=True)
class EuerQuery:
    """Der Zeitraum, den die Seite zeigt und das PDF druckt (geprüfte ISO-Daten, ``date_from`` ≤ ``date_to``)."""
    date_from: str
    date_to: str


def _period(qs: dict) -> tuple[str, str] | None:
    """``(von, bis)`` des PDF: ``von`` und ``bis`` zählen nur, wenn beide ein ISO-Datum sind (je Schlüssel genau einmal in der Adresse)
    und ``von`` nicht nach ``bis`` liegt – sonst ``None``."""
    _, from_text = _single(qs, 'from')
    _, to_text = _single(qs, 'to')
    date_from, date_to = parse_iso_date(from_text), parse_iso_date(to_text)
    if date_from is not None and date_to is not None and date_from <= date_to:
        return date_from, date_to
    return None


def read_euer_pdf_query(qs: dict) -> tuple[EuerQuery | None, str | None]:
    """Die Adresse des PDF lesen: ``(Abfrage, None)`` oder ``(None, Fehlertext)``. ``from`` und ``to`` sind Pflicht."""
    period = _period(qs)
    if period is None:
        return None, INVALID_PERIOD_ERROR
    return EuerQuery(*period), None


def _esc(value) -> str:
    """Wert aus Datenbank oder Adresse für HTML-Text und -Attribute maskieren (NULL = leer)."""
    return _html.escape(str(value if value is not None else ''), quote=True)


# Der Download: Die Adresse steht nur im Skriptblock. Nur var und function.
EUER_JS = '''
function euerPdf() { appDownload(EUER_DATA.pdf); }
'''


def _line_row(line: EuerLine) -> str:
    return f'<tr><td>{_esc(line.number)}</td><td>{_esc(line.name)}</td><td class="num">{_esc(format_amount(line.minor))}</td></tr>'


def _section(heading: str, lines: tuple[EuerLine, ...], sum_label: str, total: int) -> str:
    """Eine Gruppenüberschrift, ihre Konten und die Summe."""
    return (f'<tr><th colspan="3">{_esc(heading)}</th></tr>' + ''.join(_line_row(line) for line in lines)
            + f'<tr class="bwaSum"><td colspan="2">{_esc(sum_label)}</td><td class="num">{_esc(format_amount(total))}</td></tr>')


def _table(statement: EuerStatement) -> str:
    s = (f'<table class="reportTable"><thead><tr><th>{_esc(COLUMN_ACCOUNT)}</th><th>{_esc(COLUMN_NAME)}</th>'
         f'<th class="num">{_esc(COLUMN_AMOUNT)}</th></tr></thead><tbody>')
    s += _section(INCOME_HEADING, statement.income, INCOME_SUM_LABEL, statement.income_sum)
    s += _section(EXPENSE_HEADING, statement.expense, EXPENSE_SUM_LABEL, statement.expense_sum)
    s += (f'<tr class="bwaSum bwaResult"><td colspan="2">{_esc(RESULT_LABEL)}</td>'
          f'<td class="num">{_esc(format_amount(statement.result))}</td></tr>')
    s += _section(OTHER_HEADING, statement.other, OTHER_SUM_LABEL, statement.other_sum)
    return s + '</tbody></table>'


def PageEuer(db: Database, query: EuerQuery, today: datetime.date | None = None) -> str:
    """Die Seite zum gemeinsamen Zeitraum der Seiten (``query``); ``today`` bestimmt nur die Jahresknöpfe der Menüzeile."""
    statement = euer_statement(db.get_euer_data(query.date_from, query.date_to), bank_skr_numbers(db.fetch_accounts()))
    s = Header1('bwa')
    s += Header2(reports_submenu('euer'))
    s += Header3(period_filter_widget(query.date_from, query.date_to, '/euer', today=today))
    s += '<div class="rectRounded">'
    s += f'<h3>{_esc(EUER_TITLE)} – {_esc(range_label(query.date_from, query.date_to))}</h3>'
    s += f'<p class="hint">{_esc(EUER_HINT_TEXT)} {_esc(EUER_OTHER_TEXT)}</p>'
    s += ('<div class="rowWithObjects">'
          '<button type="button" class="coloredButton bg-blue btn-sm" onclick="euerPdf()">PDF</button>'
          '</div>')
    s += f'<div class="splitScroll">{_table(statement)}</div>'          # in einem schmalen Fenster rollt die Tabelle in sich
    s += '</div>'
    pdf_url = f'/euer/pdf?from={quote(query.date_from, safe="")}&to={quote(query.date_to, safe="")}'
    s += f'<script>\nvar EUER_DATA = {script_json({"pdf": pdf_url})};{EUER_JS}</script>'
    s += Footer()
    return s
