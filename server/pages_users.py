# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Benutzerverwaltung (TODO #4, Phase 3).

Nur für Administratoren. Erlaubt das Anlegen, Löschen,
Zurücksetzen des Passworts und Umschalten des Admin-Status von Benutzern sowie
das Ein-/Ausschalten der automatischen Anmeldung.
Verwendet ausschließlich vorhandene Style-Klassen.
"""
import html as _html
from .pages import Header1, Header2, Header3, Footer


def PageUsers(current_user, users, csrf, error_msg=None, info_msg=None, auto_login_user=None):
    """users: Liste (Username, IsAdmin, CreatedAt). current_user: angemeldeter Admin.
    auto_login_user: Administrator, der automatisch angemeldet wird (oder None)."""
    s = Header1('masterdata')
    s += Header2('<a href="/masterdata">Stammdaten</a> -> <span id="ActivePage">🔑 Benutzer</span>')
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

    # ── Automatische Anmeldung ───────────────────────────────────────────────
    c = _html.escape(csrf)
    s += '<div class="rectRounded">'
    s += '<h2>🔓 Automatische Anmeldung</h2>'
    if auto_login_user:
        a = _html.escape(auto_login_user)
        s += (f'<p>Aktiv für <strong>{a}</strong>: Wer diese Adresse erreicht, arbeitet ohne '
              f'Anmeldung als dieser Administrator. Andere Benutzer können sich so lange nicht '
              f'anmelden. Nur im eigenen Netz verwenden.</p>')
        s += f'''<form method="POST" action="/users/auto-login">
        <input type="hidden" name="csrf" value="{c}">
        <input type="hidden" name="action" value="disable">
        <table class="form-table">
          <tr><td>Neues Passwort:</td><td><input type="password" name="password" required></td></tr>
          <tr><td>Wiederholen:</td><td><input type="password" name="password2" required></td></tr>
        </table>
        <div class="rowWithObjects">
          <button type="submit" class="coloredButton bg-blue">Ausschalten und Passwort setzen</button>
        </div>
      </form>'''
    else:
        u = _html.escape(current_user or '')
        s += (f'<p>Für den Betrieb durch eine Person: <strong>{u}</strong> wird ohne Passwort '
              f'angemeldet, die Anmeldeseite entfällt. Wer diese Adresse erreicht, arbeitet dann '
              f'als Administrator; andere Benutzer können sich so lange nicht anmelden. '
              f'Nur im eigenen Netz verwenden.</p>')
        s += (f'<form method="POST" action="/users/auto-login" '
              f'onsubmit="return confirm(\'Automatische Anmeldung für {u} einschalten? '
              f'Wer diese Adresse erreicht, arbeitet dann ohne Passwort als Administrator.\')">'
              f'<input type="hidden" name="csrf" value="{c}">'
              f'<input type="hidden" name="action" value="enable">'
              f'<div class="rowWithObjects">'
              f'<button type="submit" class="coloredButton bg-orange">Für {u} einschalten</button>'
              f'</div>'
              f'</form>')
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
