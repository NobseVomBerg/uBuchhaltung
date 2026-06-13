"""
Benutzerverwaltung (TODO #4, Phase 3).

Nur für Administratoren im Mehrbenutzer-Modus. Erlaubt das Anlegen, Löschen,
Zurücksetzen des Passworts und Umschalten des Admin-Status von Benutzern.
Verwendet ausschließlich vorhandene Style-Klassen.
"""
import html as _html
from .pages import Header1, Header2, Header3, Footer


def PageUsers(current_user, users, csrf, error_msg=None, info_msg=None):
    """users: Liste (Username, IsAdmin, CreatedAt). current_user: angemeldeter Admin."""
    s = Header1('miscellaneous')
    s += Header2()
    s += Header3()

    s += '<div class="grid1Col700 gridMain">'

    if error_msg:
        s += f'<div class="rectRounded error-box">{_html.escape(error_msg)}</div>'
    if info_msg:
        s += f'<div class="rectRounded"><span class="successColor">{_html.escape(info_msg)}</span></div>'

    # ── Benutzerliste ────────────────────────────────────────────────────────
    s += '<div class="rectRounded">'
    s += '<h2>👥 Benutzerverwaltung</h2>'
    s += '<table>'
    s += '<tr><th>Benutzer</th><th>Admin</th><th>Angelegt</th><th>Aktionen</th></tr>'
    for username, is_admin, created in users:
        u = _html.escape(username)
        is_self = (username == current_user)
        admin_badge = '✓' if is_admin else '–'
        # Aktionen je Zeile (eigene kleine Formulare mit CSRF-Token)
        actions = (
            f'<form method="POST" action="/users/reset-password" class="rowWithObjects" style="display:inline-flex">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="username" value="{u}">'
            f'<input type="password" name="password" placeholder="neues Passwort" required>'
            f'<button type="submit" class="coloredButton btn-sm bg-blue">Passwort setzen</button>'
            f'</form> '
        )
        if not is_self:
            toggle_label = 'Admin entziehen' if is_admin else 'Zum Admin'
            actions += (
                f'<form method="POST" action="/users/toggle-admin" style="display:inline">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="username" value="{u}">'
                f'<button type="submit" class="coloredButton btn-sm bg-gray">{toggle_label}</button>'
                f'</form> '
                f'<form method="POST" action="/users/delete" style="display:inline" '
                f'onsubmit="return confirm(\'Benutzer {u} wirklich löschen? Die Buchungsdaten bleiben auf der Festplatte erhalten.\')">'
                f'<input type="hidden" name="csrf" value="{csrf}">'
                f'<input type="hidden" name="username" value="{u}">'
                f'<button type="submit" class="coloredButton btn-sm bg-red">Löschen</button>'
                f'</form>'
            )
        else:
            actions += '<span class="muted">(eigenes Konto)</span>'
        s += (f'<tr><td>{u}</td><td style="text-align:center">{admin_badge}</td>'
              f'<td>{_html.escape(str(created or ""))}</td><td>{actions}</td></tr>')
    s += '</table>'
    s += '</div>'

    # ── Neuen Benutzer anlegen ───────────────────────────────────────────────
    s += '<div class="rectRounded">'
    s += '<h2>Neuen Benutzer anlegen</h2>'
    s += f'''<form method="POST" action="/users/create">
        <input type="hidden" name="csrf" value="{csrf}">
        <table class="form-table">
          <tr><td>Benutzer:</td><td><input type="text" name="username" required placeholder="3–32 Zeichen: A–Z a–z 0–9 _ -"></td></tr>
          <tr><td>Passwort:</td><td><input type="password" name="password" required></td></tr>
          <tr><td>Administrator:</td><td><input type="checkbox" name="is_admin" value="1"></td></tr>
        </table>
        <div class="rowWithObjects">
          <button type="submit" class="coloredButton bg-green">Benutzer anlegen</button>
        </div>
      </form>'''
    s += '</div>'

    s += '</div>'
    s += Footer()
    return s
