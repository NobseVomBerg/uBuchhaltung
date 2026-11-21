# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Seitenübergreifender Zeitraum-Filter (Jahr / von-bis).

Der gewählte Zeitraum wird als Cookie gespeichert und gilt damit über alle
Seiten hinweg (Dashboard, Buchungen, Rechnungen, Belege, Arbeitszeiten, Fahrten und die
vier Auswertungen BWA, EÜR, Summen- und Saldenliste, Liquidität).

Auflösungs-Priorität:  explizite Query-Parameter  >  Cookie  >  Default (akt. Jahr)

Zeitraum-Werte (Query und Cookie) kommen vom Client und landen in HTML, JavaScript,
Set-Cookie-Header und Dateinamen. Deshalb gelten nur ISO-Daten (``parse_iso_date``);
alles andere wird verworfen, nicht repariert.

Die dritte Menüzeile (Von/Bis, Jahres- und Monatsknöpfe) entsteht auf jeder dieser Seiten allein
aus ``period_filter_widget`` – nie als eigenes Formular einer Seite: Auswahlfelder und farbige
Knöpfe sind höher, die Querlinie unter den Menüzeilen spränge. Es gibt drei Arten von Monatsknöpfen:
``toggle`` (Standard; ein zweiter Klick auf den aktiven Monat schaltet aufs ganze Jahr),
``always`` (Monatsauswertungen: immer genau ein Monat ist aktiv, ``period_month``) und ``none``
(Dashboard: keine Monatsknöpfe).
"""
import calendar
import html
import re
from datetime import datetime, date
from urllib.parse import quote
from http.cookies import SimpleCookie

# Monats-Kurzlabels (wie in pages_worktime.py)
_MONTHS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun',
           'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

COOKIE_NAME = 'period'

# Monatsknöpfe des Widgets: Klick auf den aktiven Monat schaltet aufs Jahr / immer ein Monat aktiv / keine
MONTHS_TOGGLE, MONTHS_ALWAYS, MONTHS_NONE = 'toggle', 'always', 'none'
YEAR_BUTTONS = 5                                # so viele Jahre (vom laufenden zurück) stehen als Knöpfe in der Zeile

# Nur ASCII-Ziffern: ``\d`` träfe in Python auch andere Ziffernschriften
_ISO_DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def parse_iso_date(value) -> str | None:
    """``value`` unverändert zurückgeben, wenn es ein ISO-Datum ``JJJJ-MM-TT`` ist
    (existierender Kalendertag, Jahr 1900–2200); sonst ``None``.

    Kein Trimmen und keine anderen Schreibweisen – was durchkommt, darf ohne weitere
    Prüfung in Adressen, Cookies und Dateinamen stehen.
    """
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return value if 1900 <= parsed.year <= 2200 else None


def valid_period(date_from, date_to) -> tuple[str, str] | None:
    """Geprüftes Paar ``(von, bis)`` oder ``None``, wenn eines kein ISO-Datum ist.
    Liegt „von“ nach „bis“, werden beide getauscht."""
    d_from, d_to = parse_iso_date(date_from), parse_iso_date(date_to)
    if d_from is None or d_to is None:
        return None
    if d_from > d_to:                            # ISO-Daten sortieren wie Kalendertage
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _read_cookie(cookie_header: str | None):
    """Zeitraum aus dem Cookie-Header lesen. Liefert (from, to) oder ('', '');
    ein Cookie mit ungültigem Zeitraum zählt als nicht vorhanden."""
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
            period = valid_period(date_from, date_to)
            if period:
                return period
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
            Die Daten sind immer gültige ISO-Daten (``valid_period``); ein
            ungültiges Query-Paar wird ignoriert.
    """
    q_from = (query_params.get('from')      or query_params.get('date_from') or [''])[0]
    q_to   = (query_params.get('to')        or query_params.get('date_to')   or [''])[0]
    period = valid_period(q_from, q_to)
    if period:
        return period[0], period[1], True

    c_from, c_to = _read_cookie(cookie_header)
    if c_from and c_to:
        return c_from, c_to, False

    year = datetime.now().year
    return f'{year}-01-01', f'{year}-12-31', False


def period_cookie_header(date_from: str, date_to: str) -> str:
    """Set-Cookie-Wert für den Zeitraum (1 Jahr gültig).

    Wirft ``ValueError``, wenn das Paar kein gültiger Zeitraum ist. Mit Werten aus
    ``resolve_period`` kommt das nicht vor – ein ungeprüfter Wert stünde sonst
    unverändert im Header und ein Jahr lang im Browser.
    """
    period = valid_period(date_from, date_to)
    if period is None:
        raise ValueError('Kein gültiger Zeitraum – erwartet werden zwei Daten im Format JJJJ-MM-TT.')
    return (f'{COOKIE_NAME}={period[0]}..{period[1]}; '
            f'Path=/; Max-Age=31536000; SameSite=Lax')


def _extra_query(extra_params: dict | None) -> str:
    """Zusätzliche, fest mitzuführende Query-Parameter als '&k=v…'-String.

    Leere/None-Werte werden ausgelassen. Ein Wert darf auch eine Liste oder ein Tupel
    sein: Der Schlüssel steht dann je Element (``acct=1&acct=2``), leere Elemente und
    leere Listen entfallen. Schlüssel und Werte werden URL-enkodiert, damit auch
    Freitext (z.B. Suchbegriffe) sicher transportiert wird.
    """
    if not extra_params:
        return ''
    parts = []
    for key, value in extra_params.items():
        for item in (value if isinstance(value, (list, tuple)) else (value,)):
            if item is None or item == '':
                continue
            parts.append(f'{quote(str(key))}={quote(str(item))}')
    return ('&' + '&'.join(parts)) if parts else ''


def _read_dates(date_from, date_to) -> tuple[date, date] | None:
    """Beide Werte als Datum; ``None``, wenn einer kein ISO-Datum ist (``parse_iso_date``)."""
    if parse_iso_date(date_from) is None or parse_iso_date(date_to) is None:
        return None
    return date.fromisoformat(date_from), date.fromisoformat(date_to)


def _is_calendar_month(d_from: date, d_to: date) -> bool:
    """Genau ein Kalendermonat: der 1. bis zum letzten Tag desselben Monats."""
    return (d_from.year == d_to.year and d_from.month == d_to.month and d_from.day == 1
            and d_to.day == calendar.monthrange(d_to.year, d_to.month)[1])


def period_month(date_from, date_to, today: date) -> tuple[int, int]:
    """(Jahr, Monat), den eine Monatsauswertung (BWA, Summen- und Saldenliste, Liquidität)
    zum gemeinsamen Zeitraum zeigt.

    - Genau ein Kalendermonat: dieser Monat.
    - Sonst Jahr und Monat des Bis-Datums – ein ganzes Jahr zeigt also seinen letzten Monat,
      den Dezember –, aber nie einen Monat in der Zukunft des laufenden Jahres: Im Jahr von
      ``today`` gilt höchstens der Monat von ``today`` (ein Jahr danach bleibt, wie es ist).
    - Ist eines der Daten kein ISO-Datum: der Monat von ``today``.
    """
    dates = _read_dates(date_from, date_to)
    if dates is None:
        return today.year, today.month
    d_from, d_to = dates
    year, month = d_to.year, d_to.month
    if not _is_calendar_month(d_from, d_to) and year == today.year and month > today.month:
        month = today.month
    return year, month


def period_filter_widget(date_from: str, date_to: str, base_path: str,
                         num_years: int = YEAR_BUTTONS, extra_params: dict | None = None,
                         months: str = MONTHS_TOGGLE, today: date | None = None) -> str:
    """Gemeinsames Header3-Fragment: Von/Bis-Felder + Jahres- + Monats-Buttons.

    Zentrale Stelle für ALLE Seiten mit Zeitraum-Filter (Dashboard, Transactions, Receipts,
    Invoice, WorkTimes, Trips und die Auswertungen BWA, EÜR, Summen- und Saldenliste,
    Liquidität). Datums-/Jahres-/Monatswechsel lösen einen Server-Reload aus
    (``base_path?from=..&to=..``), sodass nur der gewählte Zeitraum geladen/gerendert wird.
    Die Zeile steht so auf jeder Seite in derselben Höhe – deshalb nie ein eigenes Formular
    mit Auswahlfeldern oder farbigen Knöpfen an ihrer Stelle.

    Die Jahres-Buttons stehen in einem eigenen Block hinter den Von/Bis-Feldern (leicht
    abgesetzt), die Monats-Buttons in einem weiteren dahinter (bei ``months='none'`` fehlt er).

    Verhalten (analog pages_worktime.py):
      - Ist der aktive Zeitraum genau ein Kalendermonat (``single_month``), wirkt
        die Jahres-Schnellwahl monatserhaltend (gleicher Monat im anderen Jahr) – nicht bei
        ``months='none'``: Dort fehlen die Monats-Buttons, die zurück aufs Jahr führten,
        die Jahres-Buttons wählen immer das ganze Jahr.
      - Die Monats-Buttons schalten auf den jeweiligen Monat des aktiven Jahres.
      - ``months='toggle'`` (Standard): Klick auf den bereits aktiven Monat schaltet den
        Monatsfilter wieder ab und erweitert auf das ganze Jahr (Toggle).
      - ``months='always'`` (Monatsauswertungen): Es ist immer genau ein Monat aktiv, nämlich
        ``period_month(date_from, date_to, today)`` – bei einem Kalendermonat dieser, sonst der
        letzte Monat des Bis-Datums, im laufenden Jahr höchstens der laufende. Sein Button führt
        auf ihn selbst; aktiver Jahres-Button ist das Jahr dieses Monats.
      - ``months='none'`` (Dashboard): keine Monats-Buttons; aktiver Jahres-Button ist das Jahr
        des Von-Datums.

    Args:
        date_from, date_to: aktuell aktiver Zeitraum (Vorbelegung/Hervorhebung)
        base_path:          Zielpfad der Seite, z.B. '/transactions'
        num_years:          Anzahl der Jahres-Schnellwahl-Buttons (absteigend)
        extra_params:       optionale, fest mitzuführende Query-Parameter
                            (z.B. {'person': 5} oder {'status': 'paid',
                            'search': 'abc'}); leere Werte werden ausgelassen, eine Liste
                            oder ein Tupel wiederholt den Schlüssel (``acct=1&acct=2``).
        months:             ``'toggle'`` (Standard), ``'always'`` oder ``'none'``;
                            sonst ``ValueError``
        today:              „heute“ (Standard: ``date.today()``): bestimmt das laufende Jahr
                            der Jahres-Buttons und bei ``months='always'`` den Monat

    Returns:
        HTML-Fragment (rowWithObjects mit Inputs/Buttons) inkl. einmaligem
        Helper-JS. Wird von der Seite in Header3(...) eingebettet.
    """
    if months not in (MONTHS_TOGGLE, MONTHS_ALWAYS, MONTHS_NONE):
        raise ValueError(f'Unbekannte Art der Monats-Buttons: {months!r}')
    today = today or date.today()
    current_year = today.year
    extra_qs = _extra_query(extra_params)

    # Nichts davon steht roh in HTML oder JS (für gültige ISO-Daten und feste Pfade
    # ändert sich die Ausgabe nicht): Daten maskiert, weil sie in Attributen stehen;
    # der Pfad prozentkodiert, weil er in JavaScript-Zeichenketten steht – und eine
    # dort mit html.escape maskierte Anführungszeichen-Entität löst der Browser in
    # einem onclick-Attribut vor dem Ausführen wieder zu einem echten Zeichen auf.
    h_from = html.escape(str(date_from), quote=True)
    h_to = html.escape(str(date_to), quote=True)
    url_base = quote(base_path, safe='/')

    # Aktiven Zeitraum interpretieren
    d_from, d_to = _read_dates(date_from, date_to) or (date(current_year, 1, 1), date(current_year, 12, 31))
    single_month = _is_calendar_month(d_from, d_to)
    if months == MONTHS_ALWAYS:
        year, active_month = period_month(date_from, date_to, today)
    else:
        year, active_month = d_from.year, (d_from.month if single_month else None)

    def href(yf: str, yt: str) -> str:
        return f"{url_base}?from={yf}&to={yt}{extra_qs}"

    # Jahres-Buttons: monatserhaltend bei single_month, sonst ganzes Jahr (ohne Monats-Buttons immer das Jahr).
    keep_month = single_month and months != MONTHS_NONE
    year_buttons = ''
    for y in range(current_year, current_year - num_years, -1):
        if keep_month:
            last = calendar.monthrange(y, active_month)[1]
            yf, yt = f'{y}-{active_month:02d}-01', f'{y}-{active_month:02d}-{last:02d}'
        else:
            yf, yt = f'{y}-01-01', f'{y}-12-31'
        cls = " class='active'" if year == y else ''
        year_buttons += (f'<button{cls} type="button" '
                         f"onclick=\"window.location.href='{href(yf, yt)}'\">{y}</button> ")

    # Monats-Buttons fürs aktive Jahr; im Toggle-Modus führt der aktive Monat beim erneuten Klick aufs ganze Jahr.
    month_div = ''
    if months != MONTHS_NONE:
        month_buttons = ''
        for m in range(1, 13):
            is_active = active_month == m
            if is_active and months == MONTHS_TOGGLE:
                mf, mt = f'{year}-01-01', f'{year}-12-31'      # Toggle: zurück zum Jahr
            else:
                last = calendar.monthrange(year, m)[1]
                mf, mt = f'{year}-{m:02d}-01', f'{year}-{m:02d}-{last:02d}'
            cls = " class='active'" if is_active else ''
            month_buttons += (f'<button{cls} type="button" '
                              f"onclick=\"window.location.href='{href(mf, mt)}'\">{_MONTHS[m-1]}</button> ")
        month_div = f'\n        <div>{month_buttons}</div>'

    return f'''<div class="rowWithObjects">
        <div>
            <label>Von:</label>
            <input type="date" id="dateFrom" value="{h_from}" onchange="gotoPeriod()">
            <label>Bis:</label>
            <input type="date" id="dateTo" value="{h_to}" onchange="gotoPeriod()">
        </div>
        <div>{year_buttons}</div>{month_div}
    </div>
    <script>
        function gotoPeriod() {{
            const f = document.getElementById('dateFrom').value;
            const t = document.getElementById('dateTo').value;
            if (f && t) window.location.href = '{url_base}?from=' + f + '&to=' + t + '{extra_qs}';
        }}
    </script>'''
