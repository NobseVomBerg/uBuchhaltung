# uBuchhaltung im lokalen Netzwerk betreiben (Mehrbenutzer + HTTPS)

uBuchhaltung läuft als **Einzelbenutzer** (ohne Login, eine gemeinsame
`./data/buch.db`) oder als **Mehrbenutzer** im LAN (mit Login, je Nutzer ein
isoliertes Datenverzeichnis `data/users/<user>/`). Der Modus wird **bei der
Ersteinrichtung im Browser gewählt** und in `data/config.json` gespeichert –
keine Umgebungsvariable nötig.

## Betriebsmodus (`data/config.json`)

```json
{ "mode": "single" }   // oder "multi"
```

- Beim ersten Aufruf ohne vorhandene `config.json` erscheint die **Modus-Auswahl**.
- Für ein **Headless-Setup** (ohne Browser-Interaktion) kann diese Datei vorab
  mit `{"mode":"multi"}` angelegt werden.

## Netzwerk & HTTPS über Umgebungsvariablen

| Variable      | Default     | Bedeutung |
|---------------|-------------|-----------|
| `UBUCHHALTUNG_HOST` | `localhost` | Bind-Adresse. Für Netzzugriff `0.0.0.0`. |
| `UBUCHHALTUNG_PORT` | `8080`      | TCP-Port. |
| `UBUCHHALTUNG_CERT` | *(keine)*   | Pfad zur TLS-Zertifikatsdatei (PEM) ⇒ HTTPS. |
| `UBUCHHALTUNG_KEY`  | *(keine)*   | Pfad zum privaten TLS-Schlüssel (PEM). |

Sind `UBUCHHALTUNG_CERT` **und** `UBUCHHALTUNG_KEY` gesetzt, läuft der Server über HTTPS und
setzt das `Secure`-Flag der Session-Cookies.

## Erststart

1. Netz + TLS setzen und starten (Beispiel Windows/PowerShell):
   ```powershell
   $env:UBUCHHALTUNG_HOST = "0.0.0.0"
   $env:UBUCHHALTUNG_CERT = "cert.pem"
   $env:UBUCHHALTUNG_KEY  = "key.pem"
   python main.py
   ```
   Linux/macOS:
   ```bash
   UBUCHHALTUNG_HOST=0.0.0.0 UBUCHHALTUNG_CERT=cert.pem UBUCHHALTUNG_KEY=key.pem python main.py
   ```
2. Im Browser öffnen und in der **Ersteinrichtung „Mehrbenutzer"** wählen. Danach
   wird das **Administrator-Konto** angelegt (`/setup-admin`); der Admin legt unter
   **Sonstiges → Benutzerverwaltung** weitere Nutzer an. Jeder Nutzer richtet beim
   ersten Login seine eigenen Firmen-/Kontaktdaten ein.

## Schnellstart mit HTTPS (Windows)

Das Skript `run_https.ps1` erzeugt beim ersten Aufruf automatisch ein
selbst-signiertes Zertifikat (`cert.pem`/`key.pem`) und startet den Server per
HTTPS:

```powershell
.\run_https.ps1            # nur lokal (localhost)
.\run_https.ps1 -BindAll   # im LAN erreichbar (0.0.0.0)
```

`cert.pem`/`key.pem` sind per `.gitignore` ausgeschlossen und werden nie
eingecheckt.

## Selbst-signiertes Zertifikat erzeugen (manuell)

Ein selbst-signiertes Zertifikat genügt im privaten LAN (der Browser zeigt beim
ersten Besuch eine Warnung, die einmalig bestätigt wird):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
  -days 825 -subj "/CN=ubuchhaltung.local" \
  -addext "subjectAltName=IP:192.168.1.10,DNS:ubuchhaltung.local"
```

`subjectAltName` auf die tatsächliche LAN-IP / den Hostnamen des Servers setzen.

## Daten & Backup

- `data/auth.db` – zentrale Benutzer-/Session-Verwaltung.
- `data/users/<user>/` – pro Nutzer: `buch.db` sowie `logos/`, `invoices/`,
  `quotes/`, `worktime/`.
- Für ein Backup genügt es, das gesamte `data/`-Verzeichnis zu sichern.
- Das Löschen eines Nutzers in der Benutzerverwaltung entfernt nur das Login;
  das zugehörige Datenverzeichnis bleibt erhalten und kann bei Bedarf manuell
  entfernt werden.

## Dauerbetrieb (Prozess-Manager)

- **Linux:** als `systemd`-Service mit gesetzten `Environment=`-Zeilen.
- **Windows:** z. B. via [NSSM](https://nssm.cc/) als Dienst registrieren; die
  Umgebungsvariablen im Dienst hinterlegen.
- In einer VM/Container (Unraid/Proxmox/Docker): `data/` als persistentes Volume
  einbinden, `UBUCHHALTUNG_HOST=0.0.0.0` setzen, Port veröffentlichen.

## Schema-Migrationen

Bestehende Nutzer-DBs werden beim Start automatisch an die aktuelle
Schema-Version angeglichen (neue Spalten werden nachgezogen; `PRAGMA
user_version` spiegelt die `SCHEMA_VERSION` aus `version.py`). Es ist also kein
manuelles „DB löschen & neu" mehr nötig.
