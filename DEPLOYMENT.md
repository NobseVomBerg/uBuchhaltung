# PyBuch im lokalen Netzwerk betreiben (Mehrbenutzer + HTTPS)

PyBuch läuft standardmäßig als Einzelplatz ohne Login (eine gemeinsame
`./data/buch.db`). Für den Betrieb im LAN mit mehreren Nutzern wird der
Mehrbenutzer-Modus per Umgebungsvariable aktiviert; jeder Nutzer erhält dann ein
eigenes, isoliertes Datenverzeichnis unter `data/users/<user>/`.

## Konfiguration über Umgebungsvariablen

| Variable      | Default     | Bedeutung |
|---------------|-------------|-----------|
| `PYBUCH_AUTH` | *(aus)*     | `1`/`true` aktiviert Login und Datentrennung pro Nutzer. |
| `PYBUCH_HOST` | `localhost` | Bind-Adresse. Für Netzzugriff `0.0.0.0`. |
| `PYBUCH_PORT` | `8080`      | TCP-Port. |
| `PYBUCH_CERT` | *(keine)*   | Pfad zur TLS-Zertifikatsdatei (PEM) ⇒ HTTPS. |
| `PYBUCH_KEY`  | *(keine)*   | Pfad zum privaten TLS-Schlüssel (PEM). |

Sind `PYBUCH_CERT` **und** `PYBUCH_KEY` gesetzt, läuft der Server über HTTPS und
setzt das `Secure`-Flag der Session-Cookies.

## Erststart

1. Mehrbenutzer + Netz + TLS aktivieren und starten (Beispiel Windows/PowerShell):
   ```powershell
   $env:PYBUCH_AUTH = "1"
   $env:PYBUCH_HOST = "0.0.0.0"
   $env:PYBUCH_CERT = "cert.pem"
   $env:PYBUCH_KEY  = "key.pem"
   python main.py
   ```
   Linux/macOS:
   ```bash
   PYBUCH_AUTH=1 PYBUCH_HOST=0.0.0.0 PYBUCH_CERT=cert.pem PYBUCH_KEY=key.pem python main.py
   ```
2. Beim ersten Aufruf im Browser wird das **Administrator-Konto** angelegt
   (`/setup-admin`). Danach legt der Admin unter **Sonstiges → Benutzerverwaltung**
   weitere Nutzer an.

## Selbst-signiertes Zertifikat erzeugen

Ein selbst-signiertes Zertifikat genügt im privaten LAN (der Browser zeigt beim
ersten Besuch eine Warnung, die einmalig bestätigt wird):

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
  -days 825 -subj "/CN=pybuch.local" \
  -addext "subjectAltName=IP:192.168.1.10,DNS:pybuch.local"
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
  einbinden, `PYBUCH_HOST=0.0.0.0` setzen, Port veröffentlichen.

## Schema-Migrationen

Bestehende Nutzer-DBs werden beim Start automatisch an die aktuelle
Schema-Version angeglichen (neue Spalten werden nachgezogen; `PRAGMA
user_version` spiegelt die `SCHEMA_VERSION` aus `version.py`). Es ist also kein
manuelles „DB löschen & neu" mehr nötig.
