# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""
Ersteinrichtungs-Seite (First-Run Setup).

Wird angezeigt wenn noch kein eigener Kontakt und kein Bankkonto angelegt sind.
"""
import html as _html
from db import Database
from .pages import Header1, Header2, Header3, Footer


def PageSetupStart(error_msg=None, username='admin') -> str:
    """Erster Schritt der Ersteinrichtung: ersten Administrator anlegen.

    Wird nur angezeigt, solange es noch keinen Administrator gibt (frisches
    Datenverzeichnis, vorab angelegte config.json mit ``{"mode":"multi"}`` oder
    nur der Demo-Benutzer). Mit oder ohne Anmeldung – die Daten liegen danach immer
    in der Mehrbenutzer-Ablage data/users/<Name>/ (siehe handlers.handle_setup_start).
    """
    s = Header1()
    s += Header2()
    s += Header3()
    s += '<div class="grid1Col700 gridMain">'
    if error_msg:
        s += f'<div class="rectRounded error-box">{_html.escape(error_msg)}</div>'
    s += f'''
  <div class="rectRounded">
    <h2>🚀 Willkommen bei uBuchhaltung – Ersteinrichtung</h2>
    <form method="POST" action="/setup/start">
      <table class="form-table">
        <tr>
          <td>Benutzername</td>
          <td><input type="text" name="username" required value="{_html.escape(username)}"
                     placeholder="3–32 Zeichen: A–Z a–z 0–9 _ -"></td>
        </tr>
        <tr>
          <td>Passwort</td>
          <td><input type="password" name="password"></td>
        </tr>
        <tr>
          <td>Passwort wiederholen</td>
          <td><input type="password" name="password2"></td>
        </tr>
        <tr>
          <td><strong>Ohne Anmeldung</strong></td>
          <td>Für eine Person im eigenen Netz. Du arbeitest sofort los, ohne Passwort.
              Eine Anmeldung lässt sich später in der Benutzerverwaltung einschalten.</td>
        </tr>
        <tr>
          <td><strong>Mit Anmeldung</strong></td>
          <td>Mit Passwort. Weitere Benutzer mit eigenen, getrennten Büchern legst du
              in der Benutzerverwaltung an.</td>
        </tr>
      </table>
      <div class="rowWithObjects">
        <button type="submit" name="login" value="none" class="coloredButton bg-green">Ohne Anmeldung starten</button>
        <button type="submit" name="login" value="password" class="coloredButton bg-blue">Mit Passwort starten</button>
      </div>
    </form>
  </div>
</div>
'''
    s += Footer()
    return s


def PageSetup(db: Database, message: str = '') -> str:
    """Einseitige Ersteinrichtungs-Seite.

    Zwei Aktionen:
    1. Manuelles Formular (Eigene Daten + Bankkonto) → POST /setup/save
    2. Testdaten laden → POST /setup/load_testdata
    """
    s = Header1()
    s+= Header2()
    s+= Header3()

    error_html = ''
    if message:
        error_html = f'<p class="error-box">{_html.escape(str(message))}</p>'

    s += f'''
<div class="grid1Col700 gridMain">
  <div class="rectRounded">
    <h2>🚀 Willkommen bei uBuchhaltung – Ersteinrichtung</h2>
    <p>Bitte gib kurz deine eigenen Kontaktdaten und dein Bankkonto ein.
       Diese Angaben werden für Rechnungen und den DATEV-Export benötigt.</p>
    {error_html}

    <form method="POST" action="/setup/save">
      <h3>Eigene Kontaktdaten</h3>
      <table class="form-table">
        <tr>
          <td><label for="company_name">Firmenname / Name *</label></td>
          <td><input type="text" id="company_name" name="company_name" required size="40"
                     placeholder="Muster GmbH"></td>
        </tr>
        <tr>
          <td><label for="legal_form">Rechtsform</label></td>
          <td>
            <select id="legal_form" name="legal_form">
              <option value="">– keine –</option>
              <option value="GmbH">GmbH</option>
              <option value="UG (haftungsbeschränkt)">UG (haftungsbeschränkt)</option>
              <option value="AG">AG</option>
              <option value="GbR">GbR</option>
              <option value="OHG">OHG</option>
              <option value="KG">KG</option>
              <option value="e.K.">e.K.</option>
              <option value="Freiberufler">Freiberufler</option>
              <option value="Einzelunternehmen">Einzelunternehmen</option>
              <option value="Sonstiges">Sonstiges</option>
            </select>
          </td>
        </tr>
        <tr>
          <td><label for="street">Straße</label></td>
          <td><input type="text" id="street" name="street" size="40"
                     placeholder="Musterstraße 1"></td>
        </tr>
        <tr>
          <td><label for="postal_code">PLZ</label></td>
          <td><input type="text" id="postal_code" name="postal_code" size="10"
                     placeholder="12345"></td>
        </tr>
        <tr>
          <td><label for="city">Ort</label></td>
          <td><input type="text" id="city" name="city" size="30"
                     placeholder="Musterstadt"></td>
        </tr>
        <tr>
          <td><label for="tax_id">USt-ID</label></td>
          <td><input type="text" id="tax_id" name="tax_id" size="20"
                     placeholder="DE123456789"></td>
        </tr>
        <tr>
          <td><label for="email">E-Mail</label></td>
          <td><input type="email" id="email" name="email" size="40"
                     placeholder="info@meine-firma.de"></td>
        </tr>
        <tr>
          <td><label for="phone">Telefon</label></td>
          <td><input type="text" id="phone" name="phone" size="25"
                     placeholder="+49 30 12345678"></td>
        </tr>
      </table>

      <h3>Bankkonto <small class="muted">(optional, kann später ergänzt werden)</small></h3>
      <table class="form-table">
        <tr>
          <td><label for="bank_name_label">Kontobezeichnung</label></td>
          <td><input type="text" id="bank_name_label" name="bank_name_label" size="30"
                     placeholder="Geschäftskonto"></td>
        </tr>
        <tr>
          <td><label for="iban">IBAN</label></td>
          <td><input type="text" id="iban" name="iban" size="35"
                     placeholder="DE89 3704 0044 0532 0130 00"></td>
        </tr>
        <tr>
          <td><label for="bic">BIC</label></td>
          <td><input type="text" id="bic" name="bic" size="15"
                     placeholder="COBADEFFXXX"></td>
        </tr>
        <tr>
          <td><label for="bank_name">Bankname</label></td>
          <td><input type="text" id="bank_name" name="bank_name" size="30"
                     placeholder="Commerzbank AG"></td>
        </tr>
      </table>

      <br>
      <input type="submit" value="✅ Einrichtung abschließen" class="coloredButton bg-blue">
      &nbsp;&nbsp;
      <a href="/">Später einrichten →</a>
    </form>
  </div>

  <div class="rectRounded">
    <h3>🧪 Mit Testdaten starten</h3>
    <p>Lädt eine Musterfirma und ein Testkonto vor – sinnvoll für Entwicklung und Tests.
       Kann danach jederzeit in den Stammdaten angepasst werden.</p>
    <form method="POST" action="/setup/load_testdata">
      <input type="submit" value="🧪 Testdaten laden" class="coloredButton bg-orange">
    </form>
  </div>
</div>
'''
    s += Footer()
    return s
