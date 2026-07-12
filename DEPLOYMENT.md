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

## Docker (Unraid, Proxmox, NAS)

Zielplattformen sind Server, die ohnehin Docker anbieten. Das fertige Image
kommt aus der GitHub Container Registry – gebaut und veröffentlicht durch
`.github/workflows/docker-publish.yml` bei jedem Push auf `main` sowie bei
Versions-Tags (`v1.2.12`):

```
ghcr.io/nobsevomberg/ubuchhaltung:latest
```

Standardport ist **2824** („BUCH" auf der Telefontastatur – von verbreiteten
Docker-Projekten nicht belegt). Alle Nutzdaten (auth.db, Nutzer-Verzeichnisse,
Datensicherungen) liegen unter `/app/data` – für ein Backup genügt weiterhin
dieses eine Verzeichnis.

- Beim ersten Aufruf erscheint die Ersteinrichtung (Modus-Auswahl, im
  Mehrbenutzer-Modus danach `/setup-admin`). Headless-Setup: im
  Daten-Verzeichnis vorab `config.json` mit `{"mode":"multi"}` anlegen.
- Der Container startet als root, übereignet das Daten-Volume einmalig dem
  unprivilegierten App-Benutzer (UID 1000) und gibt die Rechte dann ab –
  root-eigene appdata-Verzeichnisse (Unraid) funktionieren so ohne Handarbeit.
- `seed_data/private/` (eigenes Logo/Briefpapier) ist bewusst **nicht** im
  Image; bei Bedarf als Read-only-Volume nach `/app/seed_data/private` mounten.
- HTTPS: Zertifikate mounten und `UBUCHHALTUNG_CERT`/`UBUCHHALTUNG_KEY` setzen –
  oder (üblicher) einen Reverse-Proxy (nginx Proxy Manager, Caddy, Traefik)
  davorschalten.
- 7-Zip ist im Image enthalten – Datensicherungen entstehen als `.7zip`,
  die Log-Rotation komprimiert nach `.7z`.
- GHCR-Hinweis (einmalig): Nach dem ersten Workflow-Lauf das Paket unter
  GitHub → Profil → Packages → `ubuchhaltung` → Package settings auf
  **Public** stellen, sonst verlangt der Pull eine Anmeldung.

### Unraid

Variante 1 – Template: `unraid-template.xml` aus dem Projektstamm nach
`/boot/config/plugins/dockerMan/templates-user/` kopieren, dann unter
**Docker → Add Container** das Template „uBuchhaltung" auswählen. Port und
appdata-Pfad (`/mnt/user/appdata/ubuchhaltung`) sind vorbelegt.

Variante 2 – manuell: **Docker → Add Container**, Repository
`ghcr.io/nobsevomberg/ubuchhaltung:latest`, Port `2824 → 2824`, Pfad
`/mnt/user/appdata/ubuchhaltung → /app/data`.

### Proxmox

Docker läuft unter Proxmox üblicherweise in einer VM oder einem LXC mit
Docker; dort `compose.yaml` verwenden oder direkt:

```bash
docker run -d --name ubuchhaltung --restart unless-stopped \
  -p 2824:2824 -v /opt/ubuchhaltung/data:/app/data \
  ghcr.io/nobsevomberg/ubuchhaltung:latest
```

### Selbst bauen (ohne Registry)

Im Projektverzeichnis (`Dockerfile` und `compose.yaml` liegen im Stamm):

```bash
docker compose up -d --build
```

`compose.yaml` mountet `./data` – **Container und nativen Server nie
gleichzeitig auf demselben `data/` betreiben** (SQLite-Locking).

## Dauerbetrieb (Prozess-Manager)

- **Linux:** als `systemd`-Service mit gesetzten `Environment=`-Zeilen.
- **Windows:** z. B. via [NSSM](https://nssm.cc/) als Dienst registrieren; die
  Umgebungsvariablen im Dienst hinterlegen.
- In einer VM/Container-Umgebung (Unraid/Proxmox): `data/` als persistentes
  Volume einbinden, `UBUCHHALTUNG_HOST=0.0.0.0` setzen, Port veröffentlichen –
  für Docker siehe den Abschnitt **Docker** oben.

## Schema-Migrationen

Bestehende Nutzer-DBs werden beim Start automatisch an die aktuelle
Schema-Version angeglichen (neue Spalten werden nachgezogen; `PRAGMA
user_version` spiegelt die `SCHEMA_VERSION` aus `version.py`). Es ist also kein
manuelles „DB löschen & neu" mehr nötig.
