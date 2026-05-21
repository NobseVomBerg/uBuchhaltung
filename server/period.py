"""
Seitenübergreifender Zeitraum-Filter (Jahr / von-bis).

Der gewählte Zeitraum wird als Cookie gespeichert und gilt damit über alle
Seiten hinweg (Dashboard, Transactions, Invoice, Receipts, BookingGroups).

Auflösungs-Priorität:  explizite Query-Parameter  >  Cookie  >  Default (akt. Jahr)
"""
from datetime import datetime
from http.cookies import SimpleCookie

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


def period_filter_widget(date_from: str, date_to: str, base_path: str,
                         num_years: int = 4) -> str:
    """Gemeinsames Header3-Fragment: Von/Bis-Felder + Jahres-Buttons.

    Datums-/Jahreswechsel lösen einen Server-Reload aus (``base_path?from=..&to=..``),
    sodass nur der gewählte Zeitraum geladen/gerendert wird.

    Args:
        date_from, date_to: aktuell aktiver Zeitraum (für Vorbelegung/Hervorhebung)
        base_path:          Zielpfad der Seite, z.B. '/transactions'
        num_years:          Anzahl der Jahres-Schnellwahl-Buttons (absteigend)

    Returns:
        HTML-Fragment (ein <div> mit Inputs/Buttons) inkl. einmaligem Helper-JS.
        Wird von der Seite in Header3(...) eingebettet.
    """
    current_year = datetime.now().year

    year_buttons = ''
    for y in range(current_year, current_year - num_years, -1):
        active = (date_from == f'{y}-01-01' and date_to == f'{y}-12-31')
        cls = " class='active'" if active else ''
        year_buttons += (f'<button{cls} type="button" '
                         f'onclick="setPeriodYear(\'{base_path}\', {y})">{y}</button> ')

    return f'''
        <div>
            <label>Von:</label>
            <input type="date" id="dateFrom" value="{date_from}"
                   onchange="gotoPeriod('{base_path}')">
            <label>Bis:</label>
            <input type="date" id="dateTo" value="{date_to}"
                   onchange="gotoPeriod('{base_path}')">
            {year_buttons}
        </div>
        <script>
            function gotoPeriod(base) {{
                const f = document.getElementById('dateFrom').value;
                const t = document.getElementById('dateTo').value;
                if (f && t) window.location.href = base + '?from=' + f + '&to=' + t;
            }}
            function setPeriodYear(base, y) {{
                window.location.href = base + '?from=' + y + '-01-01&to=' + y + '-12-31';
            }}
        </script>
    '''
