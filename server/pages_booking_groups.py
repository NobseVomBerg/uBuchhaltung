# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
BookingGroups pages (Split-Buchungen)

Enthält alle Seiten für die Verwaltung von Split-Buchungsgruppen:
  - PageBookingGroups       → Übersicht (Liste links) + Neu/Detail (rechts)
  - PageBookingGroupDetails → Thin-Wrapper auf PageBookingGroups(view_id=...)
"""
import html as _html
from db import Database
from .pages import Header1, Header2, Header3, Footer
from .period import period_filter_widget

# Spaltenindizes in Bookings (SELECT *):
# [0]  ID            [1]  DateBooking   [2]  DateTax       [3]  BookingGroup_ID
# [4]  Account_ID    [5]  ForeignBankAccount               [6]  RecipientClient
# [7]  Contact_ID    [8]  COA_ID        [9]  CounterCOA_ID [10] Category_ID
# [11] Amount        [12] Currency      [13] TaxRate        [14] TaxAmount
# [15] Text          [16] DocumentNumber


def PageBookingGroups(db: Database, date_from=None, date_to=None, view_id=None):
    """Split-Buchungsgruppen (grid2Cols): Liste links, Formular/Details rechts.

    Ohne Auswahl: 'Neue Gruppe'-Formular rechts. Mit view_id: Gruppe bearbeiten
    plus die zugeordneten Buchungen rechts (die bisherige Detail-Unterseite).
    """
    s = Header1('bookinggroups')
    s += Header2()
    s += Header3(period_filter_widget(date_from, date_to, '/bookinggroups'))

    view_group = None
    if view_id is not None:
        view_group = next((g for g in db.fetch_booking_groups() if g[0] == view_id), None)

    s += '<div class="grid2Cols gridMain">'
    # ── Rechte Spalte: Neu-Formular oder Gruppen-Detail ──
    s += '<div class="gridRightCol gridMiddle" style="order:2">'
    s += _booking_group_detail(db, view_group) if view_group else _booking_group_new_form()
    s += '</div><!-- Ende gridRightCol -->'

    # ── Linke Spalte: Vorhandene Gruppen ──
    s += '<div class="gridLeftCol" style="order:1">'
    groups = db.fetch_booking_groups()
    # Nur Gruppen mit mindestens einer Buchung im Zeitraum (Gruppenanzahl klein)
    if date_from and date_to:
        groups = [g for g in groups
                  if any(b[1] and date_from <= b[1] <= date_to
                         for b in db.get_bookings_in_group(g[0]))]

    if not groups:
        s += '<p>Noch keine Split-Buchungsgruppen vorhanden.</p>'
    else:
        s += '<table>'
        s += ('<tr><th>ID</th><th>Beschreibung</th><th>Erstellt</th>'
              '<th>Erwartet&nbsp;€</th><th>Tatsächlich&nbsp;€</th>'
              '<th>Buchungen</th><th>Aktionen</th></tr>')

        for group in groups:
            group_id      = group[0]
            description   = _html.escape(group[1] or '')
            created_date  = group[2] or ''
            expected      = group[3]

            bookings      = db.get_bookings_in_group(group_id)
            actual        = sum((b[11] or 0) for b in bookings)
            count         = len(bookings)

            expected_str  = f'{expected:.2f}' if expected is not None else '–'
            match_color   = ('green'
                             if expected is not None and abs(expected - actual) < 0.01
                             else 'inherit')

            s += (f'<tr><td>{group_id}</td><td>{description}</td><td>{created_date}</td>'
                  f'<td style="text-align:right">{expected_str}</td>'
                  f'<td style="text-align:right; color:{match_color}">{actual:.2f}</td>'
                  f'<td style="text-align:center">{count}</td>'
                  f'<td><a href="javascript:void(0)" onclick="openEditForm(\'/bookinggroups/view?id={group_id}\')" class="action-icon" title="Details / Bearbeiten">&#9998;</a>'
                  f' <a href="javascript:void(0);" class="action-icon delete-icon" title="Löschen"'
                  f' onclick="appConfirmHref(\'/bookinggroups/delete?id={group_id}\', \'Gruppe {group_id} löschen? Die Buchungen bleiben erhalten, werden aber aus der Gruppe gelöst.\')">&#128465;</a></td></tr>')

        s += '</table>'

    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Cols -->'
    s += '''
    <script>
        function openEditForm(url) {
            fetch(url)
                .then(r => r.text())
                .then(html => {
                    const doc = new DOMParser().parseFromString(html, 'text/html');
                    const newForm = doc.querySelector('.gridRightCol');
                    const curForm = document.querySelector('.gridRightCol');
                    if (newForm && curForm) {
                        curForm.innerHTML = newForm.innerHTML;
                        curForm.querySelectorAll('script').forEach(s => {
                            const ns = document.createElement('script');
                            ns.textContent = s.textContent;
                            s.replaceWith(ns);
                        });
                        history.pushState({}, '', url);
                    }
                })
                .catch(() => { window.location.href = url; });
        }
    </script>
    '''
    s += Footer()
    return s


def PageBookingGroupDetails(db: Database, group_id: int):
    """Thin-Wrapper – Gruppe in der rechten Spalte der kombinierten Seite."""
    return PageBookingGroups(db, view_id=group_id)


def _booking_group_new_form():
    """Render-Block: 'Neue Gruppe erstellen'-Formular (rechte Spalte)."""
    return '''
    <div class="rectRounded">
    <h2>Neue Gruppe erstellen</h2>
    <form method="POST" action="/bookinggroups/create">
        <table class="form-table">
            <tr><td>Beschreibung:</td>
                <td><input type="text" name="description" placeholder="z.B. Rechnung XY123 aufgeteilt"></td></tr>
            <tr><td>Erwarteter Gesamtbetrag:</td>
                <td><input type="number" step="0.01" name="total_amount" placeholder="Optional – für Kontrollzwecke"></td></tr>
            <tr><td></td>
                <td><input type="submit" value="Gruppe erstellen" class="coloredButton btn-sm bg-green"></td></tr>
        </table>
    </form>
    </div>
    '''


def _booking_group_detail(db: Database, group):
    """Render-Block: Gruppe bearbeiten + zugeordnete Buchungen (rechte Spalte)."""
    group_id     = group[0]
    description  = _html.escape(group[1] or '')
    created_date = group[2] or ''
    expected     = group[3]
    expected_val = f'{expected:.2f}' if expected is not None else ''

    s = '<div class="rectRounded">'
    s += f'<h2>Split-Buchung #{group_id} bearbeiten</h2>'
    s += f'''
    <form method="POST" action="/bookinggroups/update">
        <input type="hidden" name="group_id" value="{group_id}">
        <table class="form-table">
            <tr><td>Beschreibung:</td>
                <td><input type="text" name="description" value="{description}"></td></tr>
            <tr><td>Erwarteter Gesamtbetrag:</td>
                <td><input type="number" step="0.01" name="total_amount" value="{expected_val}" placeholder="Optional"></td></tr>
            <tr><td></td><td>
                <input type="submit" value="Speichern" class="coloredButton btn-sm bg-green">
                <button type="button" onclick="window.location.href='/bookinggroups'" class="coloredButton btn-sm bg-gray">Abbrechen</button>
                <button type="button" class="coloredButton btn-sm bg-red"
                    onclick="appConfirmHref('/bookinggroups/delete?id={group_id}', 'Gruppe {group_id} wirklich löschen? Buchungen bleiben erhalten.')">Gruppe löschen</button>
            </td></tr>
        </table>
    </form>
    <p class="muted">Erstellt am: {created_date}</p>
    </div>
    '''

    # ── Buchungen in der Gruppe ──
    bookings = db.get_bookings_in_group(group_id)
    s += '<div class="rectRounded"><h3>Buchungen in dieser Gruppe</h3>'
    if not bookings:
        s += '<p>Keine Buchungen zugeordnet.</p>'
    else:
        actual = sum((b[11] or 0) for b in bookings)
        expected_str = f'{expected:.2f}' if expected is not None else '–'
        diff_html    = ''
        if expected is not None and abs(expected - actual) > 0.01:
            diff = actual - expected
            diff_html = f"<span style='color:red'> (Differenz: {diff:+.2f})</span>"
        s += (f'<p><strong>Summe:</strong> {actual:.2f}&nbsp;€ &nbsp;|&nbsp; '
              f'<strong>Erwartet:</strong> {expected_str}&nbsp;€{diff_html}</p>')

        conn = db._get_connection()
        cur  = conn.cursor()
        cur.execute('SELECT ID, AccountNumber, Name FROM ChartOfAccounts')
        coa_map = {r[0]: f'{r[1]} {r[2]}' for r in cur.fetchall()}
        conn.close()

        s += ('<table><tr><th>ID</th><th>Datum</th><th>Empfänger / Text</th>'
              '<th>Konto (SKR)</th><th>Betrag&nbsp;€</th><th>Beleg-Nr.</th><th>Aktionen</th></tr>')
        for b in bookings:
            bid        = b[0]
            date_str   = b[1] or ''
            recipient  = _html.escape((b[6] or '')[:35])
            coa_id     = b[8]
            amount     = b[11] or 0
            text       = _html.escape((b[15] or '')[:40])
            doc_nr     = _html.escape(b[16] or '')
            display_text = recipient if recipient else text
            coa_label   = _html.escape(coa_map.get(coa_id, '')) if coa_id else ''
            amount_col  = 'color:green' if amount > 0 else 'color:red'
            s += (f'<tr><td>{bid}</td><td>{date_str}</td><td title="{text}">{display_text}</td>'
                  f'<td>{coa_label}</td><td style="text-align:right; {amount_col}">{amount:.2f}</td><td>{doc_nr}</td>'
                  f'<td><a href="/transactions/edit?id={bid}">Bearbeiten</a> &nbsp;|&nbsp; '
                  f'<a href="javascript:void(0);" onclick="appConfirmHref(\'/bookinggroups/unlink_booking?booking_id={bid}&group_id={group_id}\', \'Buchung {bid} aus Gruppe herauslösen?\')">Herauslösen</a></td></tr>')
        s += '</table>'
    s += '</div>'
    return s
