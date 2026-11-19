# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Fahrten-Seite (Zeiten/Fahrten – Fahrtenbuch).

Layout analog pages_worktime.py: grid2Cols – links Fahrtenliste, rechts Formular.
Personen-Filter (Fahrer) im Header2 neben dem Unter-Tab-Submenü, Zeitraum-Filter
im Header3. Startpunkt leer ⇒ eigene Adresse; km werden bei bekannter Strecke
vorausgefüllt.
"""
import calendar
import datetime
import html as _html
from urllib.parse import quote
from db import Database
from .pages import Header1, Header2, Header3, Footer, script_json
from .period import period_filter_widget
from .pages_worktime import _persons, zeiten_submenu

# Spaltenindizes Trips-Row
#   0 ID, 1 DriverID, 2 StartDate, 3 StartTime, 4 EndDate, 5 EndTime,
#   6 StartPoint, 7 Destination, 8 Vehicle, 9 Reason, 10 DistanceKm,
#   11 StartKm, 12 EndKm, 13 DocumentID, 14 CreatedAt


def _own_address(db: Database):
    """Eigene Adresse (Firmensitz) als einzeiliger String für den Startpunkt-Default."""
    own = list(db.fetch_contacts(contact_type='own'))
    if not own:
        return ''
    r = own[0]
    parts = [r[5], f"{r[6] or ''} {r[7] or ''}".strip()]   # Straße, PLZ Ort
    return ", ".join(p for p in parts if p and p.strip())


#: Hinweis im Kasten der Lückenprüfung – steht immer da, auch ohne Prüfung und ohne Fahrzeuge
_CHECK_HINT = (
    'Lückenlos muss ein Fahrtenbuch nur sein, wenn die private Nutzung nach der Fahrtenbuchmethode ermittelt wird '
    '– dann für das ganze Jahr (bzw. ab der Anschaffung oder bis zum Verkauf des Fahrzeugs). Für die pauschale '
    'Versteuerung (1 %-Regelung, bei Elektrofahrzeugen 0,25 %), die Abrechnung nach Kilometerpauschale, anderweitig '
    'abgerechnete Fahrten oder als Gedächtnisstütze genügen einzelne Fahrten. Die Prüfung hier ist ein Hilfsmittel, '
    'keine Steuerberatung.')


def _count(n, singular, plural):
    return f'{n} {singular if n == 1 else plural}'


def PageTrips(db: Database, person_id=None, date_from=None, date_to=None,
              edit_id=None, error_msg=None, check_vehicle=None, check_year=None):
    """Fahrten-Seite rendern. ``check_vehicle`` und ``check_year`` (beide nötig) lassen das Fahrtenbuch dieses
    Fahrzeugs in diesem Jahr auf Lücken prüfen; die Seite zeigt das Ergebnis im Kasten unter der Fahrtenliste."""
    persons = _persons(db)

    person_ids = [p[0] for p in persons]
    if person_id not in person_ids:
        person_id = person_ids[0] if person_ids else None

    today = datetime.date.today()
    if not date_from or not date_to:
        last = calendar.monthrange(today.year, today.month)[1]
        date_from = f'{today.year}-{today.month:02d}-01'
        date_to = f'{today.year}-{today.month:02d}-{last:02d}'

    own_address = _own_address(db)

    # Der Zeitraum kommt aus Adresse oder Cookie: In Adressen (auch innerhalb von
    # JavaScript-Zeichenketten) steht er prozentkodiert, in HTML-Attributen maskiert.
    def qs(pid=None, df=None, dt=None):
        pid = person_id if pid is None else pid
        df = date_from if df is None else df
        dt = date_to if dt is None else dt
        return f'?person={pid}&from={quote(df, safe="")}&to={quote(dt, safe="")}'

    # ── Kopfbereiche ─────────────────────────────────────────────────────────
    s = Header1('worktime')

    # Header2: Unter-Tab-Submenü + Fahrer-Auswahl
    person_opts = ''
    for p in persons:
        sel = 'selected' if p[0] == person_id else ''
        person_opts += f'<option value="{p[0]}" {sel}>{_html.escape(p[3] or f"ID {p[0]}")}</option>'
    person_select = (
        '<label>🚗 Fahrer:</label> '
        f"<select onchange=\"window.location.href='/trips?person=' + this.value "
        f"+ '&from={quote(date_from, safe='')}&to={quote(date_to, safe='')}'\">{person_opts}</select>"
    )
    s += Header2(zeiten_submenu('trips') + ' &nbsp; ' + person_select)

    # Header3: Zeitraum-Filter
    s += Header3(period_filter_widget(date_from, date_to, '/trips',
                                      extra_params={'person': person_id}))

    # ── Daten für die Tabelle aufbereiten ────────────────────────────────────
    entries = list(db.fetch_trips(person_id, date_from, date_to)) if person_id else []
    total_km = 0

    def fmt_date(value):
        try:
            return datetime.date.fromisoformat(str(value)).strftime('%d.%m.%Y')
        except (ValueError, TypeError):
            return value or ''

    def entry_row(e):
        nonlocal total_km
        km = e[10]
        if km:
            total_km += km
        start_point = _html.escape(e[6] or own_address or '–')
        dest = _html.escape(e[7] or '')
        when = fmt_date(e[2])
        if e[3]:
            when += f' {e[3]}'
        data = (f'data-id="{e[0]}" data-startdate="{e[2]}" data-starttime="{e[3] or ""}" '
                f'data-enddate="{e[4] or ""}" data-endtime="{e[5] or ""}" '
                f'data-startpoint="{_html.escape(e[6] or "")}" '
                f'data-destination="{_html.escape(e[7] or "")}" '
                f'data-vehicle="{_html.escape(e[8] or "")}" '
                f'data-reason="{_html.escape(e[9] or "")}" '
                f'data-km="{km if km is not None else ""}" '
                f'data-startkm="{e[11] if e[11] is not None else ""}" '
                f'data-endkm="{e[12] if e[12] is not None else ""}" '
                f'data-document="{e[13] if e[13] is not None else ""}"')
        return (
            f'<tr {data}>'
            f'<td>{when}</td>'
            f'<td>{start_point} → {dest}</td>'
            f'<td>{_html.escape(e[8] or "")}</td>'
            f'<td>{_html.escape(e[9] or "")}</td>'
            f'<td style="text-align:right;">{km if km is not None else ""}</td>'
            f'<td>'
            f'<a href="#" class="action-icon" title="Bearbeiten" '
            f'onclick="trEditFromRow(this); return false;">&#9998;</a> '
            f'<a href="javascript:void(0);" class="action-icon delete-icon" title="Löschen" '
            f"onclick=\"appConfirmPost('/trips/delete{qs()}&id={e[0]}', 'Fahrt wirklich löschen?')\">"
            f'&#128465;</a>'
            f'</td></tr>'
        )

    rows_html = ''.join(entry_row(e) for e in entries)
    if not rows_html:
        rows_html = ('<tr><td colspan="6" class="muted" style="text-align:center;">'
                     'Keine Fahrten im Zeitraum.</td></tr>')

    sum_row = (f'<tr class="wt-sum"><td colspan="4" style="text-align:right;">'
               f'Summe ({len(entries)} Fahrten):</td>'
               f'<td style="text-align:right;">{total_km}</td><td></td></tr>')

    # ── Zwei-Spalten-Layout ──────────────────────────────────────────────────
    s += '<div class="grid2Cols gridMain">'

    s += '<div class="gridLeftCol"><table>'
    s += ('<tr><th>Datum</th><th>Strecke</th><th>Fahrzeug</th><th>Grund</th>'
          '<th>km</th><th>Aktionen</th></tr>')
    s += rows_html
    s += sum_row
    s += '</table>'
    s += _logbook_check_box(db, person_id, date_from, date_to, today, fmt_date, check_vehicle, check_year)
    s += '</div>'

    s += _trips_form(db, person_id, date_from, date_to, own_address)

    s += '</div><!-- Ende grid2Cols -->'

    # ── Client-Daten + JS ────────────────────────────────────────────────────
    last = db.get_last_trip(person_id) if person_id else None
    template = {'vehicle': (last[8] if last else '') or ''}
    trips_data = {
        'ownAddress': own_address,
        'knownRoutes': db.get_known_routes(person_id) if person_id else {},
        'template': template,
        'today': today.isoformat(),
    }
    s += '<script>\nconst TRIPS_DATA = ' + script_json(trips_data) + ';\n'
    s += _TRIPS_JS + '</script>'

    if edit_id:
        e = db.get_trip_by_id(edit_id)
        if e and e[1] == person_id:
            preload = {
                'id': e[0], 'startdate': str(e[2]), 'starttime': e[3] or '',
                'enddate': e[4] or '', 'endtime': e[5] or '',
                'startpoint': e[6] or '', 'destination': e[7] or '',
                'vehicle': e[8] or '', 'reason': e[9] or '',
                'km': e[10] if e[10] is not None else '',
                'startkm': e[11] if e[11] is not None else '',
                'endkm': e[12] if e[12] is not None else '',
                'document': e[13] if e[13] is not None else '',
            }
            s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
                  'trLoadEntry(' + script_json(preload) + ');});</script>')

    if error_msg:
        s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
              'appMsg(' + script_json(error_msg) + ',"error");});</script>')

    s += Footer()
    return s


def _logbook_check_box(db, person_id, date_from, date_to, today, fmt_date, check_vehicle, check_year):
    """Kasten „Fahrtenbuch auf Lücken prüfen“ (linke Spalte, unter der Fahrtenliste): der feste Hinweis, ein
    GET-Formular (Fahrzeug, Jahr) und – nur nach einer Anfrage – das Ergebnis. Rein lesend."""
    esc = lambda value: _html.escape(str(value), quote=True)
    vehicles = db.get_all_vehicles()
    s = '<div class="rectRounded">'
    s += '<h2>Fahrtenbuch auf Lücken prüfen</h2>'
    s += f'<p class="hint">{esc(_CHECK_HINT)}</p>'

    asked = bool(check_vehicle and check_year)
    if vehicles:
        wanted = str(check_vehicle or '').strip().casefold()
        options = ''
        for vehicle in vehicles:
            selected = ' selected' if asked and vehicle.casefold() == wanted else ''
            options += f'<option value="{esc(vehicle)}"{selected}>{esc(vehicle)}</option>'
        # Vorbelegt mit dem Jahr der Anfrage, sonst dem Jahr des Zeitraums (nur ein Jahr aus vier ASCII-Ziffern)
        year_text = str(date_to or '')[:4]
        year = today.year
        if asked:
            year = check_year
        elif len(year_text) == 4 and year_text.isascii() and year_text.isdigit():
            year = int(year_text)
        year = min(max(int(year), 1900), 2999)
        person = '' if person_id is None else person_id
        # person, from und to halten Fahrer und Zeitraum, wenn die Seite nach dem Prüfen neu geladen wird
        s += ('<form method="GET" action="/trips" class="rowWithObjects">'
              f'<input type="hidden" name="person" value="{esc(person)}">'
              f'<input type="hidden" name="from" value="{esc(date_from)}">'
              f'<input type="hidden" name="to" value="{esc(date_to)}">'
              f'<label>Fahrzeug: <select name="check_vehicle">{options}</select></label>'
              f'<label>Jahr: <input type="number" name="check_year" min="1900" max="2999" step="1" '
              f'value="{year}"></label>'
              '<input type="submit" value="Prüfen" class="coloredButton btn-sm bg-blue">'
              '</form>')

    if asked:
        check = db.trip_logbook_check(check_vehicle, check_year)
        s += _logbook_check_result(check, check_vehicle, check_year, date_from, date_to, fmt_date)
    s += '</div>'
    return s


def _logbook_check_result(check, vehicle, year, date_from, date_to, fmt_date):
    """Ergebnis der Lückenprüfung (``Database.trip_logbook_check``) als HTML."""
    esc = lambda value: _html.escape(str(value), quote=True)
    trips, without, gaps_count = check['trips'], check['without_odometer'], check['gaps_count']
    first = '–' if check['first_start_km'] is None else f"{check['first_start_km']} km"
    last = '–' if check['last_end_km'] is None else f"{check['last_end_km']} km"
    s = f'<h3>{esc(vehicle)} im Jahr {esc(year)}</h3>'
    s += f"<p>{_count(trips, 'Fahrt', 'Fahrten')}, zusammen {check['total_km']} km.</p>"
    s += f'<p>Fahrten ohne vollständigen Tachostand: {without}</p>'
    s += f'<p>Erster Tachostand: {first}, letzter Tachostand: {last}</p>'

    if trips == 0:
        return s + '<p>Keine Fahrten mit diesem Fahrzeug im Jahr.</p>'
    if gaps_count == 0:
        if without == 0:
            return s + '<p>Keine Lücken zwischen den Tachoständen.</p>'
        return s + ('<p>Zwischen den Fahrten mit Tachostand gibt es keine Lücken – '
                    f"{_count(without, 'Fahrt hat', 'Fahrten haben')} keinen vollständigen Tachostand.</p>")

    s += ('<table><tr><th>Datum vorher</th><th>Tacho Ende</th><th>Datum danach</th><th>Tacho Start</th>'
          '<th>Differenz</th><th>Dazwischen</th><th>Aktionen</th></tr>')
    period = f"&from={quote(date_from, safe='')}&to={quote(date_to, safe='')}"
    for gap in check['gaps']:
        diff = gap['diff']
        meaning = 'fehlen' if diff > 0 else 'Überschneidung'
        between = ''
        if gap['between']:
            between = f"dazwischen {_count(gap['between'], 'Fahrt', 'Fahrten')} ohne Tachostand"
        # Adresse aus prozentkodierten Teilen: kein Zeichen, das ein Attribut beenden könnte
        edit = (f"/trips/edit?id={quote(str(gap['next_id']), safe='')}"
                f"&person={quote(str(gap['next_driver']), safe='')}{period}")
        s += (f"<tr><td>{esc(fmt_date(gap['prev_date']))}</td><td>{gap['prev_end_km']}</td>"
              f"<td>{esc(fmt_date(gap['next_date']))}</td><td>{gap['next_start_km']}</td>"
              f'<td>{diff:+d} km {meaning}</td><td>{between}</td>'
              f'<td><a href="{edit}">Bearbeiten</a></td></tr>')
    s += '</table>'
    if gaps_count > len(check['gaps']):
        s += f"<p>… und {gaps_count - len(check['gaps'])} weitere</p>"
    return s


def _trips_form(db, person_id, date_from, date_to, own_address):
    """Eingabeformular (rechte Spalte) mit Button-Leiste oben."""
    vehicles = db.get_vehicles(person_id) if person_id else []
    datalist = ''.join(f'<option value="{_html.escape(v)}">' for v in vehicles)
    ph = _html.escape(own_address) if own_address else 'eigene Adresse'

    # Beleg-Auswahl (Documents): ID, Number, Date, Filename, Path, Info
    doc_opts = '<option value="">– kein Beleg –</option>'
    for d in db.fetch_receipts():
        label = d[1] or d[3] or f'Beleg {d[0]}'      # Number, sonst Filename
        if d[2]:
            label += f' ({d[2]})'                    # Datum
        doc_opts += f'<option value="{d[0]}">{_html.escape(str(label))}</option>'

    s = '<div class="gridRightCol gridMiddle" style="order:2" id="trRightCol">'

    s += '<div class="rectRounded">'
    s += '<h2 id="trFormTitle">Neue Fahrt</h2>'
    s += f'''<form method="POST" action="/trips/add" id="trForm">
        <input type="hidden" name="id" id="trId" value="">
        <input type="hidden" name="person_id" value="{person_id}">
        <input type="hidden" name="from" value="{_html.escape(date_from, quote=True)}">
        <input type="hidden" name="to" value="{_html.escape(date_to, quote=True)}">
        <div class="rowWithObjects" id="trEditButtons" style="display:none;">
            <input type="submit" id="trSubmit" value="💾 Speichern" class="coloredButton btn-sm bg-green">
            <button type="button" id="trCopyBtn" class="coloredButton btn-sm bg-gray" onclick="trCopyAsNew()" style="display:none;" title="Diese Fahrt als Vorlage für eine neue nehmen – gespeichert wird erst mit „Speichern“">📋 Als neu kopieren</button>
            <button type="button" class="coloredButton btn-sm bg-gray" onclick="trCancel()">✖ Abbrechen</button>
        </div>
    </div>

    <div class="rectRounded">
        <table class="form-table">
            <tr><td>Start-Datum:</td><td><input type="date" name="start_date" id="trStartDate" onchange="trOnStartDateChange()"></td></tr>
            <tr><td>Start-Uhrzeit:</td><td><input type="time" name="start_time" id="trStartTime"></td></tr>
            <tr><td>End-Datum:</td><td><input type="date" name="end_date" id="trEndDate"></td></tr>
            <tr><td>End-Uhrzeit:</td><td><input type="time" name="end_time" id="trEndTime"></td></tr>
            <tr><td>Startpunkt:</td><td><input type="text" name="start_point" id="trStartPoint" maxlength="500" placeholder="{ph}" onchange="trOnRouteChange()"></td></tr>
            <tr><td>Ziel:</td><td><input type="text" name="destination" id="trDestination" maxlength="500" placeholder="Zieladresse/-ort" onchange="trOnRouteChange()"></td></tr>
            <tr><td>Fahrzeug:</td><td><input type="text" name="vehicle" id="trVehicle" maxlength="500" list="trVehicleList" placeholder="z.B. Kennzeichen"><datalist id="trVehicleList">{datalist}</datalist></td></tr>
            <tr><td>Grund:</td><td><input type="text" name="reason" id="trReason" maxlength="500" placeholder="Anlass der Fahrt"></td></tr>
            <tr><td>Tacho Start (km):</td><td><input type="number" name="start_km" id="trStartKm" min="0" step="1" placeholder="optional" onchange="trComputeKm()"></td></tr>
            <tr><td>Tacho Ende (km):</td><td><input type="number" name="end_km" id="trEndKm" min="0" step="1" placeholder="optional" onchange="trComputeKm()"></td></tr>
            <tr><td>gefahrene km:</td><td><input type="number" name="distance_km" id="trDistance" min="0" step="1" onchange="trOnKmEdit()"></td></tr>
            <tr><td>Beleg:</td><td><select name="document_id" id="trDocument">{doc_opts}</select></td></tr>
        </table>
    </form>
    </div>'''
    s += '</div><!-- Ende gridRightCol -->'
    return s


# JS als reine Zeichenkette – TRIPS_DATA wird davor injiziert.
_TRIPS_JS = r'''
function trRouteKey() {
    const start = document.getElementById('trStartPoint').value.trim().toLowerCase();
    const dest  = document.getElementById('trDestination').value.trim().toLowerCase();
    return start + '|' + dest;
}
// True, wenn beide Tachostände gesetzt und plausibel sind (Ende ≥ Start).
function trOdoActive() {
    const sk = parseInt(document.getElementById('trStartKm').value, 10);
    const ek = parseInt(document.getElementById('trEndKm').value, 10);
    return !isNaN(sk) && !isNaN(ek) && ek >= sk;
}
// Tacho-Differenz hat Vorrang: sind beide Stände gesetzt, ergibt sich die
// gefahrene Strecke automatisch und das km-Feld wird gesperrt.
function trComputeKm() {
    const dist = document.getElementById('trDistance');
    if (trOdoActive()) {
        const sk = parseInt(document.getElementById('trStartKm').value, 10);
        const ek = parseInt(document.getElementById('trEndKm').value, 10);
        dist.value = ek - sk;
        dist.readOnly = true;
        trLastAutoKm = null;
    } else {
        dist.readOnly = false;
    }
    trCheckDirty();
}
// Merkt sich den zuletzt automatisch eingetragenen km-Wert (Strecken-Vorbelegung),
// damit ein manuell gesetzter Wert nicht überschrieben wird.
let trLastAutoKm = null;
function trOnRouteChange() {
    if (trOdoActive()) { trCheckDirty(); return; }   // Tacho hat Vorrang
    const dest = document.getElementById('trDestination').value.trim();
    if (!dest) { trCheckDirty(); return; }
    const km = TRIPS_DATA.knownRoutes[trRouteKey()];
    const kmField = document.getElementById('trDistance');
    const cur = kmField.value.trim();
    if (km !== undefined && (cur === '' || cur === String(trLastAutoKm))) {
        kmField.value = km;
        trLastAutoKm = km;
    }
    trCheckDirty();
}
function trOnKmEdit() {
    trLastAutoKm = null;   // manuelle Eingabe nicht mehr überschreiben
    trCheckDirty();
}
function trOnStartDateChange() {
    // End-Datum vom Start-Datum übernehmen, solange es leer ist
    const sd = document.getElementById('trStartDate').value;
    const ed = document.getElementById('trEndDate');
    if (sd && !ed.value) ed.value = sd;
    trCheckDirty();
}
function trResetFormChrome(isNew) {
    document.getElementById('trForm').action = isNew ? '/trips/add' : '/trips/update';
    document.getElementById('trFormTitle').textContent = isNew ? 'Neue Fahrt' : 'Fahrt bearbeiten';
}
function trSnapshot() {
    return [
        document.getElementById('trId').value,
        document.getElementById('trStartDate').value,
        document.getElementById('trStartTime').value,
        document.getElementById('trEndDate').value,
        document.getElementById('trEndTime').value,
        document.getElementById('trStartPoint').value,
        document.getElementById('trDestination').value,
        document.getElementById('trVehicle').value,
        document.getElementById('trReason').value,
        document.getElementById('trStartKm').value,
        document.getElementById('trEndKm').value,
        document.getElementById('trDistance').value,
        document.getElementById('trDocument').value
    ].join('|');
}
let trBaseline = '';
function trSetBaseline() { trBaseline = trSnapshot(); }
function trCheckDirty() {
    const editing = !!document.getElementById('trId').value;
    const show = editing || (trSnapshot() !== trBaseline);
    document.getElementById('trEditButtons').style.display = show ? '' : 'none';
    // „Als neu kopieren“ gibt es nur für eine geladene Fahrt
    document.getElementById('trCopyBtn').style.display = editing ? '' : 'none';
}
// ISO-Datum als Tageszahl (UTC, ohne Sommerzeit-Sprung) bzw. zurück; kein ISO-Datum: NaN bzw. ''
function trDayNumber(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
    return m ? Math.round(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86400000) : NaN;
}
function trDayIso(number) {
    return isNaN(number) ? '' : new Date(number * 86400000).toISOString().slice(0, 10);
}
// Wie „Als neu kopieren“ der Rechnung: Die geladene Fahrt wird zur Vorlage einer neuen. Strecke, Fahrzeug, Grund, Uhrzeiten
// und Kilometer bleiben, sie beginnt heute und dauert so viele Tage wie die Vorlage. Tachostände und Beleg gehören zu
// der einen Fahrt und bleiben leer. Gespeichert wird nichts, bevor der Nutzer „Speichern“ wählt.
function trCopyAsNew() {
    if (!document.getElementById('trId').value) return;
    const start = document.getElementById('trStartDate'), end = document.getElementById('trEndDate');
    const days = trDayNumber(end.value) - trDayNumber(start.value);
    const today = trDayNumber(TRIPS_DATA.today);
    if (end.value) end.value = trDayIso(today + (days > 0 ? days : 0));
    start.value = TRIPS_DATA.today;
    document.getElementById('trId').value = '';
    document.getElementById('trStartKm').value = '';
    document.getElementById('trEndKm').value = '';
    document.getElementById('trDocument').value = '';
    document.getElementById('trDistance').readOnly = false;
    trLastAutoKm = null;
    trResetFormChrome(true);
    document.getElementById('trFormTitle').textContent = 'Neue Fahrt (Kopie)';
    trBaseline = '';            // gilt als geändert: „Speichern“ bleibt sichtbar
    trCheckDirty();
    start.focus();
}
function trCancel() { trClear(); }
function trClear() {
    ['trId','trStartDate','trStartTime','trEndDate','trEndTime',
     'trStartPoint','trDestination','trVehicle','trReason',
     'trStartKm','trEndKm','trDistance']
        .forEach(id => document.getElementById(id).value = '');
    document.getElementById('trDocument').value = '';
    document.getElementById('trDistance').readOnly = false;
    // Fahrzeug aus Vorlage (letzte Fahrt) vorbelegen
    if (TRIPS_DATA.template && TRIPS_DATA.template.vehicle) {
        document.getElementById('trVehicle').value = TRIPS_DATA.template.vehicle;
    }
    trLastAutoKm = null;
    trResetFormChrome(true);
    trSetBaseline(); trCheckDirty();
}
function trLoadEntry(d) {
    document.getElementById('trId').value = d.id;
    document.getElementById('trStartDate').value = d.startdate || '';
    document.getElementById('trStartTime').value = d.starttime || '';
    document.getElementById('trEndDate').value = d.enddate || '';
    document.getElementById('trEndTime').value = d.endtime || '';
    document.getElementById('trStartPoint').value = d.startpoint || '';
    document.getElementById('trDestination').value = d.destination || '';
    document.getElementById('trVehicle').value = d.vehicle || '';
    document.getElementById('trReason').value = d.reason || '';
    document.getElementById('trStartKm').value = (d.startkm === '' || d.startkm === undefined) ? '' : d.startkm;
    document.getElementById('trEndKm').value = (d.endkm === '' || d.endkm === undefined) ? '' : d.endkm;
    document.getElementById('trDistance').value = (d.km === '' || d.km === undefined) ? '' : d.km;
    document.getElementById('trDocument').value = (d.document === '' || d.document === undefined) ? '' : d.document;
    trLastAutoKm = null;
    trResetFormChrome(false);
    if (trOdoActive()) document.getElementById('trDistance').readOnly = true;
    else document.getElementById('trDistance').readOnly = false;
    trSetBaseline(); trCheckDirty();
}
// Dieselben Prüfungen wie der Server (_trip_fields in handlers.py), damit die Eingabe bei einem Fehler nicht verloren
// geht; maßgeblich bleibt der Server. Liefert den Text des ersten Verstoßes oder ''.
function trKmInvalid(id) {
    const v = document.getElementById(id).value.trim();
    return v !== '' && !(/^[0-9]+$/.test(v) && v.replace(/^0+/, '').length <= 7);
}
function trValidate() {
    const val = id => document.getElementById(id).value.trim();
    const sd = val('trStartDate'), ed = val('trEndDate'), st = val('trStartTime'), et = val('trEndTime');
    if (!sd || !val('trDestination')) return 'Bitte Fahrer, Start-Datum und Ziel angeben.';
    // Ein leeres End-Datum heißt: derselbe Tag
    if ((ed && ed < sd) || ((!ed || ed === sd) && st && et && et < st)) return 'Das Ende der Fahrt liegt vor dem Start.';
    if (['trStartKm', 'trEndKm', 'trDistance'].some(trKmInvalid)) return 'Kilometer bitte als ganze Zahl ab 0 angeben.';
    const sk = val('trStartKm'), ek = val('trEndKm');
    if (sk !== '' && ek !== '' && parseInt(ek, 10) < parseInt(sk, 10)) return 'Tacho Ende liegt unter Tacho Start.';
    return '';
}
function trEditFromRow(el) {
    const r = el.closest('tr');
    trLoadEntry({
        id: r.dataset.id, startdate: r.dataset.startdate, starttime: r.dataset.starttime,
        enddate: r.dataset.enddate, endtime: r.dataset.endtime,
        startpoint: r.dataset.startpoint, destination: r.dataset.destination,
        vehicle: r.dataset.vehicle, reason: r.dataset.reason, km: r.dataset.km,
        startkm: r.dataset.startkm, endkm: r.dataset.endkm, document: r.dataset.document
    });
    document.getElementById('trFormTitle').scrollIntoView({behavior:'smooth'});
}
document.addEventListener('DOMContentLoaded', function() {
    const c = document.getElementById('trRightCol');
    c.addEventListener('input', trCheckDirty);
    c.addEventListener('change', trCheckDirty);
    document.getElementById('trForm').addEventListener('submit', function(e) {
        const err = trValidate();
        if (err) { e.preventDefault(); appMsg(err, 'error'); }
    });
    if (!document.getElementById('trId').value) trClear();
});
'''
