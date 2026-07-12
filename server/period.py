# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Seitenübergreifender Zeitraum-Filter (Jahr / von-bis).

Der gewählte Zeitraum wird als Cookie gespeichert und gilt damit über alle
Seiten hinweg (Dashboard, Transactions, Invoice, Receipts, BookingGroups).

Auflösungs-Priorität:  explizite Query-Parameter  >  Cookie  >  Default (akt. Jahr)
"""
import calendar
from datetime import datetime, date
from urllib.parse import quote
from http.cookies import SimpleCookie

# Monats-Kurzlabels (wie in pages_worktime.py)
_MONTHS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun',
           'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

COOKIE_NAME = 'period'


def _read_cookie(cookie_header: str | None):
    """Zeitraum aus dem Cookie-Header lesen. Liefert (from, to) oder ('', '')."""
    if not cookie_header:
        return '', ''
    try:
        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return '', ''
    if COOKIE_NAME in jar:
        value = jar[COOKIE_NAME].value          # Format: "<from>..<to>"
        if '..' in value:
            date_from, date_to = value.split('..', 1)
            return date_from, date_to
    return '', ''


def resolve_period(query_params: dict, cookie_header: str | None):
    """Aktiven Zeitraum bestimmen.

    Args:
        query_params: aus parse_qs (Werte sind Listen). Akzeptiert ``from``/``to``
                      und ``date_from``/``date_to`` (Invoice-Schreibweise).
        cookie_header: Wert des ``Cookie``-Request-Headers (oder None).

    Returns:
        (date_from, date_to, should_set_cookie):
            should_set_cookie ist True, wenn der Zeitraum explizit per Query kam
            und daher als neuer Cookie persistiert werden soll.
    """
    q_from = (query_params.get('from')      or query_params.get('date_from') or [''])[0]
    q_to   = (query_params.get('to')        or query_params.get('date_to')   or [''])[0]
    if q_from and q_to:
        return q_from, q_to, True

    c_from, c_to = _read_cookie(cookie_header)
    if c_from and c_to:
        return c_from, c_to, False

    year = datetime.now().year
    return f'{year}-01-01', f'{year}-12-31', False


def period_cookie_header(date_from: str, date_to: str) -> str:
    """Set-Cookie-Wert für den Zeitraum (1 Jahr gültig)."""
    return (f'{COOKIE_NAME}={date_from}..{date_to}; '
            f'Path=/; Max-Age=31536000; SameSite=Lax')


def _extra_query(extra_params: dict | None) -> str:
    """Zusätzliche, fest mitzuführende Query-Parameter als '&k=v…'-String.

    Leere/None-Werte werden ausgelassen. Werte werden URL-enkodiert, damit auch
    Freitext (z.B. Suchbegriffe) sicher transportiert wird.
    """
    if not extra_params:
        return ''
    parts = []
    for key, value in extra_params.items():
        if value is None or value == '':
            continue
        parts.append(f'{quote(str(key))}={quote(str(value))}')
    return ('&' + '&'.join(parts)) if parts else ''


def period_filter_widget(date_from: str, date_to: str, base_path: str,
                         num_years: int = 4, extra_params: dict | None = None) -> str:
    """Gemeinsames Header3-Fragment: Von/Bis-Felder + Jahres- + Monats-Buttons.

    Zentrale Stelle für ALLE Seiten mit Zeitraum-Filter (Transactions, Receipts,
    BookingGroups, Invoice, WorkTimes). Datums-/Jahres-/Monatswechsel lösen einen
    Server-Reload aus (``base_path?from=..&to=..``), sodass nur der gewählte
    Zeitraum geladen/gerendert wird.

    Verhalten (analog pages_worktime.py):
      - Ist der aktive Zeitraum genau ein Kalendermonat (``single_month``), wirkt
        die Jahres-Schnellwahl monatserhaltend (gleicher Monat im anderen Jahr).
      - Die Monats-Buttons schalten auf den jeweiligen Monat des aktiven Jahres.
      - Klick auf den bereits aktiven Monat schaltet den Monatsfilter wieder ab
        und erweitert auf das ganze Jahr (Toggle).

    Args:
        date_from, date_to: aktuell aktiver Zeitraum (Vorbelegung/Hervorhebung)
        base_path:          Zielpfad der Seite, z.B. '/transactions'
        num_years:          Anzahl der Jahres-Schnellwahl-Buttons (absteigend)
        extra_params:       optionale, fest mitzuführende Query-Parameter
                            (z.B. {'person': 5} oder {'status': 'paid',
                            'search': 'abc'}); leere Werte werden ausgelassen.

    Returns:
        HTML-Fragment (rowWithObjects mit Inputs/Buttons) inkl. einmaligem
        Helper-JS. Wird von der Seite in Header3(...) eingebettet.
    """
    current_year = datetime.now().year
    extra_qs = _extra_query(extra_params)

    # Aktiven Zeitraum interpretieren
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except (TypeError, ValueError):
        d_from = date(current_year, 1, 1)
        d_to = date(current_year, 12, 31)
    single_month = (d_from.year == d_to.year and d_from.month == d_to.month
                    and d_from.day == 1
                    and d_to.day == calendar.monthrange(d_to.year, d_to.month)[1])
    year = d_from.year
    cur_month = d_from.month

    def href(yf: str, yt: str) -> str:
        return f"{base_path}?from={yf}&to={yt}{extra_qs}"

    # Jahres-Buttons: monatserhaltend bei single_month, sonst ganzes Jahr.
    year_buttons = ''
    for y in range(current_year, current_year - num_years, -1):
        if single_month:
            last = calendar.monthrange(y, cur_month)[1]
            yf, yt = f'{y}-{cur_month:02d}-01', f'{y}-{cur_month:02d}-{last:02d}'
        else:
            yf, yt = f'{y}-01-01', f'{y}-12-31'
        cls = " class='active'" if year == y else ''
        year_buttons += (f'<button{cls} type="button" '
                         f"onclick=\"window.location.href='{href(yf, yt)}'\">{y}</button> ")

    # Monats-Buttons fürs aktive Jahr; aktiver Monat erneut geklickt → ganzes Jahr.
    month_buttons = ''
    for m in range(1, 13):
        is_active = single_month and cur_month == m
        if is_active:
            mf, mt = f'{year}-01-01', f'{year}-12-31'      # Toggle: zurück zum Jahr
        else:
            last = calendar.monthrange(year, m)[1]
            mf, mt = f'{year}-{m:02d}-01', f'{year}-{m:02d}-{last:02d}'
        cls = " class='active'" if is_active else ''
        month_buttons += (f'<button{cls} type="button" '
                          f"onclick=\"window.location.href='{href(mf, mt)}'\">{_MONTHS[m-1]}</button> ")

    return f'''<div class="rowWithObjects">
        <div>
            <label>Von:</label>
            <input type="date" id="dateFrom" value="{date_from}" onchange="gotoPeriod()">
            <label>Bis:</label>
            <input type="date" id="dateTo" value="{date_to}" onchange="gotoPeriod()">
        </div>
        <div>{year_buttons}</div>
        <div>{month_buttons}</div>
    </div>
    <script>
        function gotoPeriod() {{
            const f = document.getElementById('dateFrom').value;
            const t = document.getElementById('dateTo').value;
            if (f && t) window.location.href = '{base_path}?from=' + f + '&to=' + t + '{extra_qs}';
        }}
    </script>'''
