"""
Arbeitszeiten-Seite (Zeiten/Arbeitszeiten).

Layout analog pages_receipts.py: grid2Cols – links Monatstabelle, rechts Formular.
Personen-Filter (Mitarbeiter + eigene Person), Zeitraum-Filter (Monat/Jahr).
"""
import calendar
import datetime
import html as _html
import json
from db import Database
from .pages import Header1, Header2, Header3, Footer
from .period import period_filter_widget

# Arbeitszeit-Arten (Kind) und fachliche Labels
KINDS = [
    ('work',     'Arbeit'),
    ('vacation', 'Urlaub'),
    ('sick',     'Krank'),
    ('holiday',  'Feiertag'),
]
KIND_LABELS = dict(KINDS)

WEEKDAYS = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
MONTHS = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun',
          'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez']

# Spaltenindizes WorkTimes-Row
#   0 ID, 1 PersonID, 2 Date, 3 Kind, 4 CustomerID, 5 StartTime, 6 EndTime,
#   7 PauseMinutes, 8 LocationMode, 9 LocationCity, 10 Note


def zeiten_submenu(active):
    """Gemeinsames Header2-Submenü für den Zeiten-Bereich (Arbeitszeiten / Fahrten).

    active: 'worktime' | 'trips'
    """
    def part(href, label, key):
        if key == active:
            return f'<span id="ActivePage">{label}</span>'
        return f'<a href="{href}">{label}</a>'
    return ' | '.join([
        part('/worktime', 'Arbeitszeiten', 'worktime'),
        part('/trips', 'Fahrten', 'trips'),
    ])


def compute_hours(start, end, pause_min):
    """Stunden = (Ende − Start) − Pause. Leere/ungültige Zeiten → 0.0."""
    if not start or not end:
        return 0.0
    try:
        sh, sm = (int(x) for x in str(start).split(':'))
        eh, em = (int(x) for x in str(end).split(':'))
    except (ValueError, AttributeError):
        return 0.0
    minutes = (eh * 60 + em) - (sh * 60 + sm) - int(pause_min or 0)
    if minutes < 0:
        minutes = 0
    return round(minutes / 60.0, 2)


# Externe Kontaktrollen – Personen mit (nur) solchen Rollen sind keine internen
# Personen und tauchen nicht im Arbeitszeiten-Filter auf.
_EXTERNAL_TYPE_KEYS = {
    'customer', 'supplier', 'prospect', 'partner',
    'service_provider', 'bank', 'authority', 'freelancer',
}


def _persons(db: Database):
    """Auswählbare Personen für die Arbeitszeiterfassung (dedupliziert, nach Name):

    - der eigene Kontakt ('own'),
    - alle Mitarbeiter (Typ 'employee'),
    - sonstige *interne* natürliche Personen (entity_type='person' ohne externe Rolle
      wie Kunde/Lieferant) – damit ist auch die eigene Person als Kontakt wählbar.
    """
    seen = {}
    for r in db.fetch_contacts(contact_type='own'):
        seen[r[0]] = r
    for r in db.fetch_contacts(contact_type='employee'):
        seen[r[0]] = r
    for r in db.fetch_contacts(entity_type='person'):
        type_keys = {t for t in (r[27] or '').split(',') if t}
        if not (type_keys & _EXTERNAL_TYPE_KEYS):   # rein interne Person
            seen[r[0]] = r
    return sorted(seen.values(), key=lambda r: (r[3] or '').lower())


def _hhmm(value):
    return value or ''


def PageWorkTimes(db: Database, person_id=None, date_from=None, date_to=None,
                  edit_id=None, error_msg=None):
    """Arbeitszeiten-Seite rendern."""
    persons = _persons(db)

    # Personenauswahl absichern: gültige ID oder erste Person
    person_ids = [p[0] for p in persons]
    if person_id not in person_ids:
        person_id = person_ids[0] if person_ids else None

    # Zeitraum-Defaults (aktueller Monat), falls nicht gesetzt
    today = datetime.date.today()
    if not date_from or not date_to:
        last = calendar.monthrange(today.year, today.month)[1]
        date_from = f'{today.year}-{today.month:02d}-01'
        date_to = f'{today.year}-{today.month:02d}-{last:02d}'

    d_from = datetime.date.fromisoformat(date_from)
    d_to = datetime.date.fromisoformat(date_to)
    single_month = (d_from.year == d_to.year and d_from.month == d_to.month)

    customers = list(db.fetch_contacts(contact_type='customer'))
    own_rows = list(db.fetch_contacts(contact_type='own'))
    own_city = (own_rows[0][7] if own_rows else '') or ''
    customer_city = {str(c[0]): (c[7] or '') for c in customers}

    def qs(pid=None, df=None, dt=None):
        pid = person_id if pid is None else pid
        df = date_from if df is None else df
        dt = date_to if dt is None else dt
        return f'?person={pid}&from={df}&to={dt}'

    # ── Kopfbereiche ─────────────────────────────────────────────────────────
    s = Header1('worktime')

    # Header2: Personen-Filter
    person_opts = ''
    for p in persons:
        sel = 'selected' if p[0] == person_id else ''
        person_opts += f'<option value="{p[0]}" {sel}>{_html.escape(p[3] or f"ID {p[0]}")}</option>'
    person_select = (
        '<label>👤 Person:</label> '
        f"<select onchange=\"window.location.href='/worktime?person=' + this.value "
        f"+ '&from={date_from}&to={date_to}'\">{person_opts}</select>"
    )
    s += Header2(zeiten_submenu('worktime') + ' &nbsp; ' + person_select)

    # Header3: zentraler Zeitraum-Filter (Von/Bis + Jahr + Monat inkl. Monats-
    # Toggle). Person wird als Zusatzparameter mitgeführt.
    s += Header3(period_filter_widget(date_from, date_to, '/worktime',
                                      extra_params={'person': person_id}))

    # ── Daten für die Tabelle aufbereiten ────────────────────────────────────
    entries = list(db.fetch_worktimes(person_id, date_from, date_to)) if person_id else []
    by_date = {}
    for e in entries:
        by_date.setdefault(str(e[2]), []).append(e)

    total_hours = 0.0
    workday_count = 0

    def date_label(d: datetime.date):
        return f'{d.day:02d}' if single_month else d.strftime('%d.%m')

    def entry_row(d: datetime.date, e):
        nonlocal total_hours, workday_count
        weekend = d.weekday() >= 5
        wd = WEEKDAYS[d.weekday()]
        kind = e[3] or 'work'
        start = _hhmm(e[5])
        end = _hhmm(e[6])
        pause = e[7] or 0
        hours = compute_hours(start, end, pause) if kind == 'work' else 0.0
        if kind == 'work' and hours > 0:
            total_hours += hours
            workday_count += 1
        info = '' if kind == 'work' else KIND_LABELS.get(kind, kind)
        if kind == 'work' and e[9]:
            info = _html.escape(e[9])           # Arbeitsort als Info bei Arbeit
        note = _html.escape(e[10] or '')
        row_cls = ' class="wt-weekend"' if weekend else ''
        pausetext = e[11] if len(e) > 11 else ''
        data = (f'data-id="{e[0]}" data-date="{e[2]}" data-kind="{kind}" '
                f'data-customer="{e[4] or ""}" data-start="{start}" data-end="{end}" '
                f'data-pause="{pause}" data-locmode="{e[8] or "customer"}" '
                f'data-loccity="{_html.escape(e[9] or "")}" '
                f'data-note="{_html.escape(e[10] or "")}" '
                f'data-pausetext="{_html.escape(pausetext or "")}"')
        hours_txt = f'{hours:.2f}'.replace('.', ',') if kind == 'work' else '–'
        return (
            f'<tr{row_cls} {data}>'
            f'<td>{wd}</td><td>{date_label(d)}</td>'
            f'<td>{start}</td><td>{end}</td><td>{pause if start else ""}</td>'
            f'<td style="text-align:right;">{hours_txt}</td>'
            f'<td>{info}</td><td>{note}</td>'
            f'<td>'
            f'<a href="#" class="action-icon" title="Bearbeiten" '
            f'onclick="wtEditFromRow(this); return false;">&#9998;</a> '
            f'<a href="javascript:void(0);" class="action-icon delete-icon" title="Löschen" '
            f"onclick=\"appConfirmHref('/worktime/delete{qs()}&id={e[0]}', 'Eintrag wirklich löschen?')\">"
            f'&#128465;</a>'
            f'</td></tr>'
        )

    def empty_row(d: datetime.date):
        weekend = d.weekday() >= 5
        wd = WEEKDAYS[d.weekday()]
        row_cls = ' class="wt-weekend"' if weekend else ''
        return (
            f'<tr{row_cls}>'
            f'<td>{wd}</td><td>{date_label(d)}</td>'
            f'<td></td><td></td><td></td><td></td><td></td><td></td>'
            f'<td><a href="#" class="action-icon" title="Eintrag anlegen" '
            f"onclick=\"wtNewForDay('{d.isoformat()}'); return false;\">&#10010;</a></td>"
            f'</tr>'
        )

    # Tabellenzeilen erzeugen
    rows_html = ''
    if single_month:
        for day in range(d_from.day, d_to.day + 1):
            d = datetime.date(d_from.year, d_from.month, day)
            day_entries = by_date.get(d.isoformat(), [])
            if day_entries:
                for e in day_entries:
                    rows_html += entry_row(d, e)
            else:
                rows_html += empty_row(d)
    else:
        for e in entries:
            d = datetime.date.fromisoformat(str(e[2]))
            rows_html += entry_row(d, e)

    total_txt = f'{total_hours:.2f}'.replace('.', ',')
    sum_row = (f'<tr class="wt-sum"><td colspan="5" style="text-align:right;">'
               f'Summe ({workday_count} Arbeitstage):</td>'
               f'<td style="text-align:right;">{total_txt}</td>'
               f'<td colspan="3"></td></tr>')

    # ── Zwei-Spalten-Layout ──────────────────────────────────────────────────
    s += '<div class="grid2Cols gridMain">'

    # LINKS: Tabelle
    s += '<div class="gridLeftCol"><table>'
    s += ('<tr><th>Tag</th><th>Datum</th><th>Start</th><th>Ende</th><th>Pause</th>'
          '<th>Std.</th><th>Info</th><th>Notiz</th><th>Aktionen</th></tr>')
    s += rows_html
    s += sum_row
    s += '</table></div>'

    # RECHTS: Formular
    s += _worktime_form(db, person_id, date_from, date_to, customers, edit_id, qs())

    s += '</div><!-- Ende grid2Cols -->'

    # ── Client-Daten + JS ────────────────────────────────────────────────────
    last = db.get_last_worktime_for_person(person_id) if person_id else None
    template = None
    if last:
        template = {
            'customer': str(last[4] or ''),
            'start': last[5] or '',
            'end': last[6] or '',
            'pause': last[7] or 0,
            'locmode': last[8] or 'customer',
            'loccity': last[9] or '',
            'pausetext': (last[11] if len(last) > 11 else '') or '',
        }
    wt_data = {
        'customerCity': customer_city,
        'ownCity': own_city,
        'template': template,
        'today': today.isoformat(),
        'pdfBase': f'/worktime/pdf?person={person_id}&from={date_from}&to={date_to}',
    }
    s += '<script>\nconst WT_DATA = ' + json.dumps(wt_data, ensure_ascii=False) + ';\n'
    s += _WORKTIME_JS + '</script>'

    # Bei /worktime/edit: Eintrag direkt ins Formular laden
    if edit_id:
        e = db.get_worktime_by_id(edit_id)
        if e and e[1] == person_id:
            preload = {
                'id': e[0], 'date': str(e[2]), 'kind': e[3] or 'work',
                'customer': str(e[4] or ''), 'start': e[5] or '', 'end': e[6] or '',
                'pause': e[7] or 0, 'locmode': e[8] or 'customer',
                'loccity': e[9] or '', 'note': e[10] or '',
                'pausetext': (e[11] if len(e) > 11 else '') or '',
            }
            s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
                  'wtLoadEntry(' + json.dumps(preload, ensure_ascii=False) + ');});</script>')

    # Fehlermeldung (z.B. Überschneidung) über die appMsg-Bar einblenden
    if error_msg:
        s += ('<script>document.addEventListener("DOMContentLoaded",function(){'
              'appMsg(' + json.dumps(error_msg, ensure_ascii=False) + ',"error");});</script>')

    s += Footer()
    return s


def _worktime_form(db, person_id, date_from, date_to, customers, edit_id, qstr):
    """Eingabeformular (rechte Spalte) mit Button-Leiste oben + PDF-Export."""
    customer_opts = '<option value="">– kein Kunde –</option>'
    for c in customers:
        customer_opts += f'<option value="{c[0]}">{_html.escape(c[3] or f"ID {c[0]}")}</option>'

    kind_opts = ''.join(f'<option value="{k}">{lbl}</option>' for k, lbl in KINDS)

    pdf_link = f'/worktime/pdf?person={person_id}&from={date_from}&to={date_to}'

    # Zwei-Box-Aufbau wie /masterdata/articles: oben Titel + Buttons (eigene Box),
    # darunter das Formular (eigene Box); das <form> umspannt beide Boxen.
    s = '<div class="gridRightCol gridMiddle" style="order:2" id="wtRightCol">'

    # Box 1: Titel + Aktions-Buttons (separat über dem Formular)
    s += '<div class="rectRounded">'
    s += '<h2 id="wtFormTitle">Neuer Eintrag</h2>'
    s += f'''<form method="POST" action="/worktime/add" id="wtForm">
        <input type="hidden" name="id" id="wtId" value="">
        <input type="hidden" name="person_id" value="{person_id}">
        <input type="hidden" name="from" value="{date_from}">
        <input type="hidden" name="to" value="{date_to}">
        <div class="rowWithObjects">
            <button type="button" class="coloredButton btn-sm bg-indigo" onclick="wtOpenPdf()" title="Angezeigten Zeitraum als PDF">📄 PDF</button>
            <label><input type="checkbox" id="wtPdfNotes"> Mit Notiz erstellen</label>
        </div>
        <div class="rowWithObjects" id="wtEditButtons" style="display:none;">
            <input type="submit" id="wtSubmit" value="💾 Speichern" class="coloredButton btn-sm bg-green">
            <button type="button" class="coloredButton btn-sm bg-gray" onclick="wtCancel()">✖ Abbrechen</button>
        </div>
    </div>

    <!-- Box 2: Formularfelder -->
    <div class="rectRounded">
        <table class="form-table">
            <tr><td>Datum:</td><td><input type="date" name="date" id="wtDate" onchange="wtOnDateChange()"></td></tr>
            <tr><td>Art:</td><td><select name="kind" id="wtKind" onchange="wtOnKindChange()">{kind_opts}</select></td></tr>
            <tr><td>Kunde:</td><td><select name="customer_id" id="wtCustomer" onchange="wtOnCustomerChange()">{customer_opts}</select></td></tr>
            <tr><td>Startzeit:</td><td><input type="time" name="start_time" id="wtStart" onchange="wtUpdateHours()"></td></tr>
            <tr><td>Endzeit:</td><td><input type="time" name="end_time" id="wtEnd" onchange="wtUpdateHours()"></td></tr>
            <tr><td>Pausen:</td><td><input type="text" name="pause_text" id="wtPauseText" placeholder="z.B. 12:00-12:30" onchange="wtOnPauseText()"></td></tr>
            <tr><td>Pause (min):</td><td><input type="number" name="pause_minutes" id="wtPause" min="0" value="0" onchange="wtUpdateHours()"></td></tr>
            <tr><td>Stunden:</td><td><span id="wtHoursPreview" class="muted">0,00</span></td></tr>
            <tr><td>Arbeitsort:</td><td>
                <label><input type="radio" name="location_mode" value="own" onchange="wtOnLocModeChange()"> eigener Ort</label><br>
                <label><input type="radio" name="location_mode" value="customer" checked onchange="wtOnLocModeChange()"> Ort des Kunden</label><br>
                <label><input type="radio" name="location_mode" value="other" onchange="wtOnLocModeChange()"> Sonstiger:</label>
                <input type="text" name="location_city" id="wtLocCity" placeholder="Stadt/Dorf">
            </td></tr>
            <tr><td>Notiz:</td><td><input type="text" name="note" id="wtNote" placeholder="Was wurde gemacht?"></td></tr>
        </table>
    </form>
    </div>'''
    s += '</div><!-- Ende gridRightCol -->'
    return s


# JS als reine Zeichenkette (keine f-String-Klammern) – WT_DATA wird davor injiziert.
_WORKTIME_JS = r'''
function wtSetLocCity() {
    const mode = document.querySelector('input[name="location_mode"]:checked').value;
    const city = document.getElementById('wtLocCity');
    if (mode === 'own') {
        city.value = WT_DATA.ownCity; city.readOnly = true;
    } else if (mode === 'customer') {
        const cid = document.getElementById('wtCustomer').value;
        city.value = WT_DATA.customerCity[cid] || ''; city.readOnly = true;
    } else {
        city.readOnly = false;
    }
}
function wtOnLocModeChange() { wtSetLocCity(); }
function wtOnCustomerChange() {
    const mode = document.querySelector('input[name="location_mode"]:checked').value;
    if (mode === 'customer') wtSetLocCity();
}
function wtOnKindChange() {
    const work = document.getElementById('wtKind').value === 'work';
    ['wtStart','wtEnd','wtPause'].forEach(id => document.getElementById(id).disabled = !work);
    if (!work) document.getElementById('wtHoursPreview').textContent = '0,00';
    else wtUpdateHours();
}
function wtUpdateHours() {
    const s = document.getElementById('wtStart').value;
    const e = document.getElementById('wtEnd').value;
    const p = parseInt(document.getElementById('wtPause').value || '0', 10);
    let h = 0;
    if (s && e) {
        const [sh, sm] = s.split(':').map(Number);
        const [eh, em] = e.split(':').map(Number);
        let min = (eh*60+em) - (sh*60+sm) - p;
        if (min < 0) min = 0;
        h = min / 60;
    }
    document.getElementById('wtHoursPreview').textContent = h.toFixed(2).replace('.', ',');
}
function wtPauseMinutesFromText(txt) {
    // Summiert alle Zeitbereiche, z.B. "12:00-12:30" oder "10-10:15, 12:00-12:30"
    let total = 0;
    const re = /(\d{1,2})(?::(\d{2}))?\s*(?:-|–|bis)\s*(\d{1,2})(?::(\d{2}))?/g;
    let m;
    while ((m = re.exec(txt)) !== null) {
        const s = parseInt(m[1], 10) * 60 + (m[2] ? parseInt(m[2], 10) : 0);
        const e = parseInt(m[3], 10) * 60 + (m[4] ? parseInt(m[4], 10) : 0);
        if (e > s) total += (e - s);
    }
    return total;
}
function wtOnPauseText() {
    // Pausentext automatisch in Minuten umrechnen (Anforderung 5)
    const txt = document.getElementById('wtPauseText').value;
    const min = wtPauseMinutesFromText(txt);
    if (min > 0) document.getElementById('wtPause').value = min;
    wtUpdateHours();
}
function wtOpenPdf() {
    let url = WT_DATA.pdfBase;
    if (document.getElementById('wtPdfNotes').checked) url += '&notes=1';
    window.location.href = url;
}
function wtSetMode(mode) {
    const r = document.querySelector('input[name="location_mode"][value="'+mode+'"]');
    if (r) r.checked = true;
}
function wtResetFormChrome(isNew) {
    document.getElementById('wtForm').action = isNew ? '/worktime/add' : '/worktime/update';
    document.getElementById('wtFormTitle').textContent = isNew ? 'Neuer Eintrag' : 'Eintrag bearbeiten';
}
// ── Dirty-Erkennung: Speichern/Abbrechen nur bei Änderungen einblenden ────
let wtBaseline = '';
function wtSnapshot() {
    const mode = (document.querySelector('input[name="location_mode"]:checked')||{}).value || '';
    return [
        document.getElementById('wtId').value,
        document.getElementById('wtDate').value,
        document.getElementById('wtKind').value,
        document.getElementById('wtCustomer').value,
        document.getElementById('wtStart').value,
        document.getElementById('wtEnd').value,
        document.getElementById('wtPause').value,
        mode,
        document.getElementById('wtLocCity').value,
        document.getElementById('wtNote').value,
        document.getElementById('wtPauseText').value
    ].join('|');
}
function wtSetBaseline() { wtBaseline = wtSnapshot(); }
function wtCheckDirty() {
    // Beim Bearbeiten eines vorhandenen Eintrags sofort sichtbar; beim Neuanlegen
    // erst, sobald etwas geändert wurde.
    const editing = !!document.getElementById('wtId').value;
    const show = editing || (wtSnapshot() !== wtBaseline);
    document.getElementById('wtEditButtons').style.display = show ? '' : 'none';
}
function wtCancel() {
    // Abbrechen → Formular leeren (zurück zum leeren Neu-Zustand)
    wtClear();
}
function wtClear() {
    // Formular vollständig leeren (neuer Eintrag, ohne Vorlage)
    document.getElementById('wtId').value = '';
    document.getElementById('wtDate').value = '';
    document.getElementById('wtKind').value = 'work';
    document.getElementById('wtCustomer').value = '';
    document.getElementById('wtStart').value = '';
    document.getElementById('wtEnd').value = '';
    document.getElementById('wtPause').value = 0;
    document.getElementById('wtPauseText').value = '';
    document.getElementById('wtNote').value = '';
    wtSetMode('customer');
    wtOnKindChange(); wtSetLocCity(); wtUpdateHours();
    wtResetFormChrome(true);
    wtSetBaseline(); wtCheckDirty();
}
function wtApplyTemplate() {
    // Vorlage (letzter Eintrag) in noch leere Felder übernehmen
    const t = WT_DATA.template;
    if (!t) return;
    const cust = document.getElementById('wtCustomer');
    const st = document.getElementById('wtStart');
    const en = document.getElementById('wtEnd');
    const pa = document.getElementById('wtPause');
    if (!cust.value) cust.value = t.customer || '';
    if (!st.value)   st.value   = t.start  || '';
    if (!en.value)   en.value   = t.end    || '';
    if (!pa.value || pa.value === '0') pa.value = t.pause || 0;
    const pt = document.getElementById('wtPauseText');
    if (!pt.value) pt.value = t.pausetext || '';
    wtSetMode(t.locmode || 'customer');
    wtSetLocCity(); wtUpdateHours();
}
function wtOnDateChange() {
    // Vorlage nur bei NEUEM Eintrag und erst nach Datumseingabe (Anforderung 1.1)
    if (!document.getElementById('wtId').value && document.getElementById('wtDate').value) {
        wtApplyTemplate();
    }
    wtCheckDirty();
}
function wtNewForDay(dateStr) {
    // Schnell-Anlegen für einen konkreten Tag (Vorlage sofort anwenden)
    wtClear();
    document.getElementById('wtDate').value = dateStr;
    wtApplyTemplate();
    wtCheckDirty();
    document.getElementById('wtFormTitle').scrollIntoView({behavior:'smooth'});
}
function wtLoadEntry(d) {
    document.getElementById('wtId').value = d.id;
    document.getElementById('wtDate').value = d.date;
    document.getElementById('wtKind').value = d.kind;
    document.getElementById('wtCustomer').value = d.customer || '';
    document.getElementById('wtStart').value = d.start || '';
    document.getElementById('wtEnd').value = d.end || '';
    document.getElementById('wtPause').value = d.pause || 0;
    document.getElementById('wtPauseText').value = d.pausetext || '';
    wtSetMode(d.locmode || 'customer');
    document.getElementById('wtNote').value = d.note || '';
    wtOnKindChange();
    // bei 'other' den gespeicherten Ort übernehmen, sonst aus Modus ableiten
    if ((d.locmode||'customer') === 'other') {
        document.getElementById('wtLocCity').readOnly = false;
        document.getElementById('wtLocCity').value = d.loccity || '';
    } else { wtSetLocCity(); }
    wtUpdateHours();
    wtResetFormChrome(false);
    wtSetBaseline(); wtCheckDirty();
}
function wtEditFromRow(el) {
    const r = el.closest('tr');
    wtLoadEntry({
        id: r.dataset.id, date: r.dataset.date, kind: r.dataset.kind,
        customer: r.dataset.customer, start: r.dataset.start, end: r.dataset.end,
        pause: r.dataset.pause, locmode: r.dataset.locmode,
        loccity: r.dataset.loccity, note: r.dataset.note, pausetext: r.dataset.pausetext
    });
    document.getElementById('wtFormTitle').scrollIntoView({behavior:'smooth'});
}
// Initialzustand + Dirty-Listener. Wichtig: am Spalten-Container lauschen, nicht am
// <form> – die Felder liegen (Zwei-Box-Layout) außerhalb des form-DOM-Teilbaums.
document.addEventListener('DOMContentLoaded', function() {
    const c = document.getElementById('wtRightCol');
    c.addEventListener('input', wtCheckDirty);
    c.addEventListener('change', wtCheckDirty);
    if (!document.getElementById('wtId').value) wtClear();
});
'''
