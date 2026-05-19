"""
BookingGroups pages (Split-Buchungen)

Enthält alle Seiten für die Verwaltung von Split-Buchungsgruppen:
  - PageBookingGroups       → Übersicht + Neue Gruppe anlegen
  - PageBookingGroupDetails → Detailansicht + Bearbeiten + Buchungen verwalten
"""
from db import Database
from .pages import Header1, Header2, Header3, Footer

# Spaltenindizes in Bookings (SELECT *):
# [0]  ID            [1]  DateBooking   [2]  DateTax       [3]  BookingGroup_ID
# [4]  Account_ID    [5]  ForeignBankAccount               [6]  RecipientClient
# [7]  Contact_ID    [8]  COA_ID        [9]  CounterCOA_ID [10] Category_ID
# [11] Amount        [12] Currency      [13] TaxRate        [14] TaxAmount
# [15] Text          [16] DocumentNumber


def PageBookingGroups(db: Database):
    """Übersichtsliste aller Split-Buchungsgruppen mit Anlegen-Formular."""
    s = Header1('bookinggroups')
    s += Header2()
    s += Header3()

    s += '<div class="grid2Rows">'
    s += '<div class="gridRightCol" style="order:2"><div class="rectRounded">'
    # ── Neue Gruppe anlegen ───────────────────────────────────────────────
    s += '''
    <h2>Neue Gruppe erstellen</h2>
    <form method="POST" action="/bookinggroups/create">
        <table class="form-table">
            <tr>
                <td>Beschreibung:</td>
                <td><input type="text" name="description" size="60"
                    placeholder="z.B. Rechnung XY123 aufgeteilt"></td>
            </tr>
            <tr>
                <td>Erwarteter Gesamtbetrag:</td>
                <td><input type="number" step="0.01" name="total_amount"
                    placeholder="Optional – für Kontrollzwecke"></td>
            </tr>
            <tr>
                <td></td>
                <td><input type="submit" value="Gruppe erstellen"></td>
            </tr>
        </table>
    </form>
    '''

    # ── Vorhandene Gruppen ────────────────────────────────────────────────
    s += '</div></div><!-- Ende gridRightCol -->'
    s += '<div class="gridLeftCol" style="order:1">'
    groups = db.fetch_booking_groups()

    if not groups:
        s += '<p>Noch keine Split-Buchungsgruppen vorhanden.</p>'
    else:
        # COA-Map für Kontobezeichnungen
        conn = db._get_connection()
        cur = conn.cursor()
        cur.execute('SELECT AccountNumber, Name FROM ChartOfAccounts')
        coa_names = {r[0]: r[1] for r in cur.fetchall()}
        conn.close()

        s += '<table>'
        s += ('<tr><th>ID</th><th>Beschreibung</th><th>Erstellt</th>'
              '<th>Erwartet&nbsp;€</th><th>Tatsächlich&nbsp;€</th>'
              '<th>Buchungen</th><th>Aktionen</th></tr>')

        for group in groups:
            group_id      = group[0]
            description   = group[1] or ''
            created_date  = group[2] or ''
            expected      = group[3]

            bookings      = db.get_bookings_in_group(group_id)
            actual        = sum((b[11] or 0) for b in bookings)
            count         = len(bookings)

            expected_str  = f'{expected:.2f}' if expected is not None else '–'
            match_color   = ('green'
                             if expected is not None and abs(expected - actual) < 0.01
                             else 'inherit')

            s += f'''<tr>
                <td>{group_id}</td>
                <td>{description}</td>
                <td>{created_date}</td>
                <td style="text-align:right">{expected_str}</td>
                <td style="text-align:right; color:{match_color}">{actual:.2f}</td>
                <td style="text-align:center">{count}</td>
                <td>
                    <a href="/bookinggroups/view?id={group_id}">Details</a>
                    &nbsp;|&nbsp;
                    <a href="javascript:void(0);"
                       onclick="appConfirmHref('/bookinggroups/delete?id={group_id}', 'Gruppe {group_id} löschen? Die Buchungen bleiben erhalten, werden aber aus der Gruppe gelöst.')">
                       Löschen</a>
                </td>
            </tr>'''

        s += '</table>'

    s += '</div><!-- Ende gridLeftCol --></div><!-- Ende grid2Rows -->'
    s += Footer()
    return s


def PageBookingGroupDetails(db: Database, group_id: int):
    """Detailansicht einer Split-Buchungsgruppe.

    Zeigt:
    - Bearbeitungsformular für Gruppenkopf (Beschreibung, Erwartungsbetrag)
    - Tabelle aller Buchungen in der Gruppe mit Herauslöse- und Bearbeitungslinks
    - Lösch-Button für die gesamte Gruppe
    """
    # ── Gruppe laden ──────────────────────────────────────────────────────
    groups = db.fetch_booking_groups()
    group  = next((g for g in groups if g[0] == group_id), None)

    s = Header1('bookinggroups')
    s += Header2()
    s += Header3()

    if not group:
        s += '<h1>Gruppe nicht gefunden</h1>'
        s += "<p><a href='/bookinggroups'>← Zurück zur Übersicht</a></p>"
        s += Footer()
        return s

    description  = group[1] or ''
    created_date = group[2] or ''
    expected     = group[3]

    s += f'<h1>Split-Buchung #{group_id}</h1>'

    # ── Bearbeitungsformular ──────────────────────────────────────────────
    s += '<h2>Gruppe bearbeiten</h2>'
    expected_val = f'{expected:.2f}' if expected is not None else ''
    s += f'''
    <form method="POST" action="/bookinggroups/update">
        <input type="hidden" name="group_id" value="{group_id}">
        <table class="form-table">
            <tr>
                <td>Beschreibung:</td>
                <td><input type="text" name="description" size="60"
                    value="{description}"></td>
            </tr>
            <tr>
                <td>Erwarteter Gesamtbetrag:</td>
                <td><input type="number" step="0.01" name="total_amount"
                    value="{expected_val}" placeholder="Optional"></td>
            </tr>
            <tr>
                <td></td>
                <td>
                    <input type="submit" value="Speichern">
                    &nbsp;
                    <a href="javascript:void(0);"
                       onclick="appConfirmHref('/bookinggroups/delete?id={group_id}', 'Gruppe {group_id} wirklich l\u00f6schen? Buchungen bleiben erhalten.')"
                       class="btn-delete">Gruppe löschen</a>
                </td>
            </tr>
        </table>
    </form>
    <p style="color:#888; font-size:0.9em">Erstellt am: {created_date}</p>
    '''

    # ── Buchungen ─────────────────────────────────────────────────────────
    bookings = db.get_bookings_in_group(group_id)

    s += '<h2>Buchungen in dieser Gruppe</h2>'

    if not bookings:
        s += '<p>Keine Buchungen zugeordnet.</p>'
    else:
        actual = sum((b[11] or 0) for b in bookings)

        # Zusammenfassung
        expected_str = f'{expected:.2f}' if expected is not None else '–'
        diff_html    = ''
        if expected is not None and abs(expected - actual) > 0.01:
            diff = actual - expected
            diff_html = f"<span style='color:red'> (Differenz: {diff:+.2f})</span>"
        s += f'''<p>
            <strong>Summe:</strong> {actual:.2f}&nbsp;€
            &nbsp;|&nbsp;
            <strong>Erwartet:</strong> {expected_str}&nbsp;€
            {diff_html}
        </p>'''

        # Hilfsmaps
        accounts = db.fetch_accounts()
        acc_map  = {a[0]: a[1] for a in accounts}

        conn = db._get_connection()
        cur  = conn.cursor()
        cur.execute('SELECT ID, AccountNumber, Name FROM ChartOfAccounts')
        coa_map = {r[0]: f'{r[1]} {r[2]}' for r in cur.fetchall()}
        conn.close()

        s += '''<table>
            <tr>
                <th>ID</th><th>Datum</th><th>Empfänger / Text</th>
                <th>Konto (SKR)</th><th>Betrag&nbsp;€</th><th>Beleg-Nr.</th>
                <th>Aktionen</th>
            </tr>'''

        for b in bookings:
            bid        = b[0]
            date_str   = b[1] or ''
            account_id = b[4]
            recipient  = (b[6] or '')[:35]
            coa_id     = b[8]
            amount     = b[11] or 0
            text       = (b[15] or '')[:40]
            doc_nr     = b[16] or ''

            display_text = recipient if recipient else text
            acc_name    = acc_map.get(account_id, '') if account_id else ''
            coa_label   = coa_map.get(coa_id, '') if coa_id else ''
            amount_col  = 'color:green' if amount > 0 else 'color:red'

            s += f'''<tr>
                <td>{bid}</td>
                <td>{date_str}</td>
                <td title="{text}">{display_text}</td>
                <td>{coa_label}</td>
                <td style="text-align:right; {amount_col}">{amount:.2f}</td>
                <td>{doc_nr}</td>
                <td>
                    <a href="/transactions/edit?id={bid}">Bearbeiten</a>
                    &nbsp;|&nbsp;
                    <a href="javascript:void(0);"
                       onclick="appConfirmHref('/bookinggroups/unlink_booking?booking_id={bid}&group_id={group_id}', 'Buchung {bid} aus Gruppe herauslösen?')">Herauslösen</a>
                </td>
            </tr>'''

        s += '</table>'

    s += "<p><a href='/bookinggroups'>← Zurück zur Übersicht</a></p>"
    s += Footer()
    return s
