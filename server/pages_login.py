"""
Login- und Bootstrap-Seiten für den Mehrbenutzer-Betrieb (TODO #4).

Eigenständige, schlanke Seiten ohne die normale Navigation – sie werden vor der
Anmeldung ausgeliefert. Es werden ausschließlich vorhandene Style-Klassen genutzt
(grid1Col700, rectRounded, form-table, error-box, coloredButton, muted).
"""
import html as _html
from version import APP_VERSION


def _shell(title, body):
    """Minimales HTML-Gerüst ohne Navigationsleiste (vor dem Login)."""
    return (
        "<!DOCTYPE html>\n<html>\n <head>\n  <meta charset='UTF-8'>\n"
        f"  <title>{_html.escape(title)}</title>\n"
        "  <link rel='stylesheet' href='/buch.css'>\n"
        "  <link rel='icon' sizes='32x32' href='favicon.ico'>\n"
        " </head>\n <body>\n"
        "  <div class='grid1Col700 gridMain'>\n" + body + "\n  </div>\n </body>\n</html>"
    )


def PageLogin(error_msg=None, username=""):
    """Anmeldeseite."""
    err = (f'<p class="error-box rectRounded">{_html.escape(error_msg)}</p>' if error_msg else '')
    body = f'''
    <div class="rectRounded">
      <h2>PyBuch – Anmeldung</h2>
      {err}
      <form method="POST" action="/login">
        <table class="form-table">
          <tr><td>Benutzer:</td><td><input type="text" name="username" value="{_html.escape(username)}" autofocus required></td></tr>
          <tr><td>Passwort:</td><td><input type="password" name="password" required></td></tr>
        </table>
        <div class="rowWithObjects">
          <input type="submit" value="Anmelden" class="coloredButton bg-green">
        </div>
      </form>
      <p class="muted">PyBuch {APP_VERSION}</p>
    </div>'''
    return _shell("PyBuch – Anmeldung", body)


def PageSetupAdmin(error_msg=None, username=""):
    """Erst-Einrichtung: erstes Administrator-Konto anlegen."""
    err = (f'<p class="error-box rectRounded">{_html.escape(error_msg)}</p>' if error_msg else '')
    body = f'''
    <div class="rectRounded">
      <h2>PyBuch – Erster Start</h2>
      <p>Es existiert noch kein Benutzer. Lege ein Administrator-Konto an.</p>
      {err}
      <form method="POST" action="/setup-admin">
        <table class="form-table">
          <tr><td>Benutzer:</td><td><input type="text" name="username" value="{_html.escape(username)}" autofocus required></td></tr>
          <tr><td>Passwort:</td><td><input type="password" name="password" required></td></tr>
          <tr><td>Passwort (Wdh.):</td><td><input type="password" name="password2" required></td></tr>
        </table>
        <div class="rowWithObjects">
          <input type="submit" value="Administrator anlegen" class="coloredButton bg-green">
        </div>
      </form>
      <p class="muted">PyBuch {APP_VERSION}</p>
    </div>'''
    return _shell("PyBuch – Erster Start", body)
