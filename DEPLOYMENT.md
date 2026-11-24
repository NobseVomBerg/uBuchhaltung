# uBuchhaltung betreiben (Netzwerk, Docker, Mehrbenutzer, HTTPS)

uBuchhaltung legt die Daten jedes Benutzers isoliert unter `data/users/<user>/` ab. Bei der **Ersteinrichtung im
Browser** wird der erste Administrator angelegt – **mit Passwort** oder **ohne Anmeldung** (der Administrator wird dann
automatisch angemeldet, das ist der Betrieb für eine Person im eigenen Netz). Das Ergebnis steht in `data/config.json`;
eine Umgebungsvariable ist dafür nicht nötig. Die App ist für den Betrieb im eigenen Netz gebaut, nicht fürs offene Internet.

Installationen aus Version 1.x (Daten direkt in `data/`) zieht der erste Start von Version 2 automatisch um – siehe
[Umzug auf Version 2](#umzug-auf-version-2).

Die Schutzmaßnahmen des Servers (Anmeldung, Anfragegrößen, SQL-Konsole, Zeitgrenzen) stehen in
`docs/regeln/sicherheit.md`, Sicherung und Wiederherstellung in `docs/regeln/sicherung.md`. Hier steht nur, was
beim Betrieb zu beachten ist.

## Konfiguration (`data/config.json`)

```json
{ "mode": "multi", "auto_login": "admin" }
```

- `"mode": "multi"` markiert die Installation als eingerichtet. Die Ersteinrichtung trägt es ein; im Browser lässt es
  sich danach nicht mehr ändern. Jeder andere Wert gilt als „nicht eingerichtet“.
- `auto_login` (nur bei `"multi"`): Dieser Administrator wird ohne Passwort angemeldet, die Anmeldeseite entfällt und andere
  Benutzer können sich so lange nicht anmelden. Unter *Stammdaten → Benutzer* lässt sich die automatische Anmeldung
  ein- und ausschalten; beim Ausschalten wird ein Passwort gesetzt.
- **Headless-Setup:** `config.json` vorab mit `{"mode":"multi"}` anlegen; die Ersteinrichtung (`/setup`) legt dann den
  Administrator an – mit oder ohne Anmeldung.
- Fehlt die Datei oder ist sie nicht lesbar, obwohl es schon einen Administrator gibt (z. B. nach einem Tippfehler beim
  Bearbeiten von Hand), zeigt die App einen Hinweis statt der Ersteinrichtung. Dann die Datei wiederherstellen – mit
  `{"mode": "multi"}` gilt die Anmeldung mit Passwort.

## Umgebungsvariablen

| Variable | Default | Bedeutung |
|---|---|---|
| `UBUCHHALTUNG_HOST` | `localhost` (Image: `0.0.0.0`) | Bind-Adresse. Für Netzzugriff `0.0.0.0`. |
| `UBUCHHALTUNG_PORT` | `8080` (Image: `2824`) | TCP-Port. |
| `UBUCHHALTUNG_CERT` | *(keine)* | Pfad zur TLS-Zertifikatsdatei (PEM) ⇒ HTTPS. |
| `UBUCHHALTUNG_KEY` | *(keine)* | Pfad zum privaten TLS-Schlüssel (PEM). |
| `UBUCHHALTUNG_ALLOWED_HOSTS` | *(leer)* | Zusätzliche Hostnamen (Kommaliste), unter denen die App ohne Anmeldung erreichbar sein darf – siehe [Hostnamen](#hostnamen-und-reverse-proxy). |
| `UBUCHHALTUNG_DEMO` | *(aus)* | `1`, `true`, `yes` oder `on`: [Demo-Modus](#demo-modus-ausprobieren). |
| `SMTP_HOST` / `SMTP_PORT` | `smtp.gmail.com` / `587` | Ausgangsserver für den E-Mail-Versand von Rechnungen (Port 465: TLS von Anfang an, jeder andere: STARTTLS). |
| `SMTP_USER` / `SMTP_PASSWORD` | *(leer)* | Zugangsdaten für den Versand; ohne sie lehnt die App den Versand mit einem Hinweis ab. |
| `SSL_CERT_FILE` | *(keine)* | CA-Datei (PEM) für einen Mailserver mit eigener Zertifizierungsstelle, siehe unten. |

Das Docker-Image setzt zusätzlich `PYTHONUNBUFFERED=1`. Die Variablen `UBUCHHALTUNG_MCP_*` gehören zur
[Diagnose-Schnittstelle](#diagnose-schnittstelle-mcp-nur-lesend) und werden nicht auf der Instanz gesetzt.

**HTTPS:** Sind `UBUCHHALTUNG_CERT` **und** `UBUCHHALTUNG_KEY` gesetzt, läuft der Server über HTTPS und setzt das
`Secure`-Flag der Session-Cookies. Dasselbe gilt hinter einem Reverse-Proxy, der HTTPS entgegennimmt und das mit
`X-Forwarded-Proto: https` meldet (nginx Proxy Manager, Caddy und Traefik tun das von sich aus; bei einem selbst
geschriebenen nginx: `proxy_set_header X-Forwarded-Proto $scheme;`).

**E-Mail-Versand:** Die App verschlüsselt die Verbindung zum Mailserver immer und prüft dessen Zertifikat samt Hostnamen;
das lässt sich nicht abschalten. Stammt das Zertifikat von einer eigenen Zertifizierungsstelle (z. B. ein Mailserver im LAN),
macht man diese dem Container über `SSL_CERT_FILE` bekannt: das CA-Zertifikat als PEM-Datei einbinden und ihren Pfad
eintragen, z. B. `-v /opt/ubuchhaltung/ca.pem:/certs/ca.pem:ro -e SSL_CERT_FILE=/certs/ca.pem` (Compose: unter `volumes`
und `environment`). Scheitert die Prüfung, meldet die App „Das Zertifikat des Mailservers konnte nicht geprüft werden.“;
Einzelheiten stehen im Server-Log.

## Hostnamen und Reverse-Proxy

- Ohne Login (Ersteinrichtung, automatische Anmeldung, Demo-Modus) antwortet die App nur unter IP-Adressen, `localhost`,
  Namen ohne Punkt, Namen mit den Endungen `.local`, `.lan`, `.home.arpa`, `.internal`, `.fritz.box`, `.localhost` und dem
  eigenen Rechnernamen (Schutz vor DNS-Rebinding). Für andere Namen (eigene Domain am Reverse-Proxy, Tailscale-Name):
  ```bash
  UBUCHHALTUNG_ALLOWED_HOSTS=buch.example.org,server.tailnet.ts.net
  ```
- Reverse-Proxy: `Host` durchreichen oder `X-Forwarded-Host` setzen (nginx Proxy Manager, Caddy und Traefik tun das von
  sich aus); sonst den öffentlichen Namen in `UBUCHHALTUNG_ALLOWED_HOSTS` eintragen.
- **Größe von Uploads:** Die App nimmt Uploads bis 200 MB an (Logos 5 MB), die WISO-Datenbank (*Daten → Import aus der
  Datenbank*) bis 2 GB je Datei, und nur, wenn im Datenverzeichnis danach noch 64 MB frei bleiben. Ein Reverse-Proxy hat
  oft eine kleinere eigene Grenze (nginx: `client_max_body_size`, Standard 1 MB); sie muss mindestens so groß sein wie die
  größte Datei, die hochgeladen werden soll.
- **Zeitgrenzen:** Der Server schließt eine Verbindung, auf der 120 Sekunden lang nichts gelesen oder gesendet wird; die
  Bearbeitung einer Anfrage selbst hat keine Zeitgrenze. Ein Reverse-Proxy sollte ungenutzte Verbindungen zur App kürzer
  offen halten (z. B. 60 Sekunden). Beim Herunterladen großer Sicherungen darf er die Antwort nicht nach kurzer Zeit
  abbrechen (nginx: `proxy_read_timeout`).
- Anmeldung: Nach fünf Fehlversuchen in Folge ist ein Benutzername zeitweise gesperrt (je Name, nicht je Adresse – hinter
  einem Proxy teilen sich alle dieselbe). Ein Neustart des Servers hebt die Sperre auf. Ein neues Passwort braucht
  mindestens 8 Zeichen.

## Erststart (ohne Docker)

Voraussetzung: Python 3.13 oder neuer, `pip install -r requirements.txt`.

1. Netz + TLS setzen und starten (Windows/PowerShell):
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
2. Im Browser öffnen: Die **Ersteinrichtung** legt das Administrator-Konto an. Der Administrator legt unter
   *Stammdaten → Benutzer* weitere Nutzer an. Jeder Nutzer richtet beim ersten Login seine eigenen Firmen- und
   Kontaktdaten ein.

**Schnellstart mit HTTPS (Windows):** `run_https.ps1` erzeugt beim ersten Aufruf ein selbst-signiertes Zertifikat
(`cert.pem`/`key.pem`, per `.gitignore` ausgeschlossen; braucht `openssl`) und startet den Server per HTTPS:

```powershell
.\run_https.ps1            # nur lokal (localhost)
.\run_https.ps1 -BindAll   # im LAN erreichbar (0.0.0.0)
```

**Zertifikat von Hand erzeugen:** Ein selbst-signiertes Zertifikat genügt im privaten LAN (der Browser warnt beim ersten
Besuch einmalig). `subjectAltName` auf die tatsächliche LAN-IP bzw. den Hostnamen des Servers setzen:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem \
  -days 825 -subj "/CN=ubuchhaltung.local" \
  -addext "subjectAltName=IP:192.168.1.10,DNS:ubuchhaltung.local"
```

## Daten und Sicherung

- `data/auth.db` – zentrale Benutzer- und Sitzungsverwaltung.
- `data/users/<user>/` – je Nutzer: `buch.db`, `Belege/`, `invoices/`, `quotes/`, `worktime/`, `logos/`, `backup/`, die
  SQL-Protokolle (`sql_operations.log`/`.sql`) und, wenn angelegt, `db-export.sql`. `upload_tmp/` hält nur Zwischendateien;
  die Datensicherung der App lässt den Ordner aus.
- Für ein Backup von außen genügt es, das gesamte `data/`-Verzeichnis zu sichern (Server vorher stoppen oder die
  Sicherung der App benutzen; eine laufende Datenbank nicht einfach kopieren).
- *Daten → Datensicherung* legt Archive an und bietet sie samt den Kopien der Datenbank vor einer Programm-Umstellung zum
  Herunterladen an; die Wiederherstellung steht auf derselben Seite. Auf einen Datenordner gehört nur ein Serverprozess.
  Bleibt nach einem Absturz mitten im Wiederherstellen ein Ordner `backup/replaced_…` zurück, enthält er den Stand von
  davor – von dort zurückkopieren und den Ordner erst danach löschen. Regeln: `docs/regeln/sicherung.md`.
- Ist ein Ordner im Datenverzeichnis nur ein Link auf einen anderen Ort (z. B. Belege auf einem NAS), ersetzt „vorher
  löschen“ bei der Wiederherstellung den Link durch einen normalen Ordner; das Linkziel bleibt unberührt. Als eigenes
  Volume eingebundene Ordner bleiben eingebunden.
- Das Löschen eines Nutzers in der Benutzerverwaltung entfernt nur das Login; sein Datenverzeichnis bleibt erhalten und
  kann bei Bedarf von Hand entfernt werden.
- Die **SQL-Konsole** und der **DB-Export** stehen auf der Seite *About* (ganz unten) und nur Administratoren zur
  Verfügung; die Konsole arbeitet nur auf der eigenen Datenbank des angemeldeten Nutzers und läuft lesend, solange
  „Schreiben erlauben“ nicht angekreuzt ist. Vor Änderungen eine Datensicherung anlegen. Regeln und Grenzen:
  `docs/regeln/sicherheit.md`.
- Schema-Migrationen: Bestehende Datenbanken werden beim Start automatisch auf den aktuellen Stand gebracht. Davor legt die
  App `backup/JJJJMMTT_vor_<Version>_buch.db` im Datenordner des Nutzers an; ein Zurück auf eine ältere Version gibt es nur
  über diese Sicherung (Anleitung im README, Abschnitt „Gut zu wissen“). Eine Datenbank von einer neueren Version öffnet die
  App nicht (`docs/regeln/versionierung.md`).

## Docker (Unraid, Proxmox, NAS)

Das fertige Image kommt aus der GitHub Container Registry, gebaut und veröffentlicht durch
`.github/workflows/docker-publish.yml`:

```
ghcr.io/nobsevomberg/ubuchhaltung:latest
```

| Tag | Inhalt |
|---|---|
| `:latest` | neuester Release (empfohlen) |
| `:2.9.1`, `:2.9`, `:2` (Beispiel) | fester Release bzw. neuester innerhalb der Reihe – zum Festhalten einer Version (nach einer Schemaänderung führt nur die Sicherung zurück) |
| `:edge` | aktueller Stand von `main`, auch ohne Versionssprung |

Ein Versionssprung in `version.py` auf `main` ist der Release: Tests, Image, Tag und GitHub-Release entstehen automatisch.
Änderungen ohne neue Version (z. B. Doku) bauen nur `:edge`.

- Standardport ist **2824** („BUCH“ auf der Telefontastatur). Alle Nutzdaten liegen unter `/app/data` – für ein Backup
  genügt dieses eine Verzeichnis.
- Beim ersten Aufruf erscheint die Ersteinrichtung. Headless-Setup: im Datenverzeichnis vorab `config.json` mit
  `{"mode":"multi"}` anlegen. Von Version 1.x: siehe [Umzug](#umzug-auf-version-2).
- Der Container startet als root, übereignet das Daten-Volume einmalig dem unprivilegierten App-Benutzer (UID 1000) und gibt
  die Rechte dann ab – root-eigene appdata-Verzeichnisse (Unraid) funktionieren so ohne Handarbeit. Mit `--user` gestartet,
  läuft der Container direkt unter diesem Benutzer.
- Der Healthcheck des Images fragt die Startseite ab (bei gesetztem `UBUCHHALTUNG_CERT` über HTTPS).
- Das Firmenlogo lädt man in den Stammdaten beim eigenen Kontakt hoch (PNG, JPG, GIF oder WEBP bis 5 MB); es landet in
  `users/<Name>/logos/`. Ein von Hand eingetippter Serverpfad ergibt kein Logo.
- `seed_data/private/` (eigenes Logo/Briefpapier) ist bewusst **nicht** im Image; bei Bedarf als Read-only-Volume nach
  `/app/seed_data/private` mounten.
- HTTPS: Zertifikate mounten und `UBUCHHALTUNG_CERT`/`UBUCHHALTUNG_KEY` setzen – oder (üblicher) einen Reverse-Proxy davorschalten.
- 7-Zip ist im Image enthalten – Datensicherungen entstehen als `.7zip`, die Log-Rotation komprimiert nach `.7z`.

### Unraid

- **Community Applications:** im Tab **Apps** nach „uBuchhaltung“ suchen und installieren. Port und appdata-Pfad
  (`/mnt/user/appdata/ubuchhaltung`) sind vorbelegt.
- **Template von Hand:** `unraid-template.xml` aus dem Projektstamm nach `/boot/config/plugins/dockerMan/templates-user/`
  kopieren, dann unter **Docker → Add Container** das Template „uBuchhaltung“ auswählen.
- **Manuell:** **Docker → Add Container**, Repository `ghcr.io/nobsevomberg/ubuchhaltung:latest`, Port `2824 → 2824`, Pfad
  `/mnt/user/appdata/ubuchhaltung → /app/data`.

### Proxmox

Docker läuft unter Proxmox üblicherweise in einer VM oder einem LXC mit Docker; dort `compose.yaml` verwenden oder direkt:

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

`compose.yaml` mountet `./data` – **Container und nativen Server nie gleichzeitig auf demselben `data/` betreiben**
(SQLite-Locking).

## Dauerbetrieb ohne Docker

- **Linux:** als `systemd`-Service mit gesetzten `Environment=`-Zeilen.
- **Windows:** z. B. via [NSSM](https://nssm.cc/) als Dienst registrieren; die Umgebungsvariablen im Dienst hinterlegen.

## Belegerkennung

Die Seite *Belege* liest hochgeladene Belege aus: E-Rechnungen (XRechnung, ZUGFeRD) und PDFs mit Textebene; für Scans und
Fotos gibt es keine Texterkennung. Dafür startet die App je Beleg einen kurzlebigen Python-Kindprozess (derselbe Interpreter
und dieselben Pakete – keine zusätzliche Installation). Er hat ein hartes Zeitlimit von 15 Sekunden; unter Linux, also auch
im Container, gilt zusätzlich eine Speichergrenze von 768 MB. Je Serverprozess läuft höchstens ein Lesen zurzeit, Dateien über
30 MB werden nicht gelesen. Die Lesungen stehen in der Datenbank (Tabelle `DocumentReadings`) und gehören zur Datensicherung.
Im Demo-Modus ist das Lesen abgeschaltet. Regeln: `docs/regeln/belege.md`.

## Diagnose-Schnittstelle (MCP, nur lesend)

Mit `tools/mcp_ubuchhaltung.py` lässt sich eine laufende Instanz (z. B. der Docker-Container im LAN) von einem MCP-fähigen
Client untersuchen, ohne Browser. Der Server läuft **lokal auf dem Rechner des Clients** (der Client startet ihn als
stdio-MCP-Server) und fragt die Instanz per HTTP ab. Auf dem NAS bzw. im Container ist nichts zu installieren; nötig sind
nur Python (ab 3.13) und die Standardbibliothek. Aufbau und Sperren: `docs/regeln/werkzeuge.md`.

**Fünf Werkzeuge:**

| Werkzeug | Zweck |
|---|---|
| `info` | Adresse, Anmeldung und Version der Instanz |
| `sql` | genau ein `SELECT` in der Datenbank des angemeldeten Kontos |
| `db_check` | `quick_check` (mit `full`: `integrity_check`), Fremdschlüssel, Schema-Version, Journal-Modus, Tabellenzahl |
| `schema` | alle Tabellen mit Zeilenzahl; mit Tabellenname: `CREATE`-Anweisung, Spalten, Fremdschlüssel, Indizes |
| `page` | eine geprüfte Anzeigeseite der App per GET, als Text (z. B. `/about`, `/invoice?id=3`, `/users`) |

`sql` gibt zuerst die Spaltennamen als JSON-Array aus, dann jede Zeile als JSON-Array. **Geldbeträge stehen als Ganzzahl in
Minor Units** (Skalierung 4: `1190000` = 119,00 €). Gleich benannte Spalten benennt SQLite um (`ID`, `ID:1`) – besser mit
`AS` unterscheiden. BLOB-Spalten lassen sich nur über `hex(x)` oder `length(x)` abfragen.

**Grenzen:**

- Nur ein einzelnes `SELECT` ohne `;`; kein `WITH`/`RECURSIVE`, keine Abfragen über 20 000 Zeichen. Seiten nur aus einer
  Positivliste (`_PAGE_ALLOWLIST` in `tools/mcp_ubuchhaltung.py`); Aktionen, PDF-Erzeugung, `/login` und `/logout` fehlen.
- Außer der Anmeldung und der SQL-Konsole (`/execute_sql`) sendet er keine POSTs und nie die Freigabe zum Schreiben.
  Antworten der Instanz begrenzt er auf 16 MiB. Das Passwort steht nie in Ausgaben oder Logs.
- **Restrisiko:** Die SQL-Konsole der Instanz bricht jede Anfrage nach 60 Sekunden ab, aber nicht jede aufwendige Abfrage
  (etwa Kreuzprodukte großer Tabellen) wird vorher abgewiesen. Solche Abfragen belasten die Instanz und blockieren
  Schreibzugriffe, bis sie enden.

**Voraussetzung:** ein Konto mit Administratorrechten (die SQL-Konsole steht nur Administratoren zur Verfügung). Bei
automatischer Anmeldung braucht der Server keine Zugangsdaten; dort gilt die Hostnamen-Prüfung aus
[Hostnamen](#hostnamen-und-reverse-proxy).

**Einrichtung unter Windows (PowerShell):**

1. Zugangsdaten ablegen. `cmdkey` fragt das Passwort ab; es liegt danach verschlüsselt in der
   Windows-Anmeldeinformationsverwaltung:
   ```powershell
   cmdkey /generic:uBuchhaltung-MCP /user:<Benutzer> /pass
   ```
2. Server registrieren. Der lokale Scope hält die Adresse außerhalb des Repos. **Empfohlen ist HTTPS** (`https://…`, bei
   eigenem Zertifikat zusätzlich `--env UBUCHHALTUNG_MCP_CAFILE=<Pfad>`). Über `http://` sendet der Server Benutzername und
   Passwort nur an den eigenen Rechner (`localhost`, `127.0.0.1`, `::1`). Im reinen LAN ohne HTTPS muss die Ausnahme bewusst
   gesetzt werden; Passwort und Sitzung gehen dann unverschlüsselt durchs Netz:
   ```powershell
   claude mcp add --scope local ubuchhaltung --env UBUCHHALTUNG_MCP_URL=http://nas.local:2824 --env UBUCHHALTUNG_MCP_ALLOW_HTTP=1 -- python <Projektordner>\tools\mcp_ubuchhaltung.py
   ```
   Bei automatischer Anmeldung der Instanz werden keine Zugangsdaten gesendet; dort braucht es die Ausnahme nicht.
3. Optional ohne Nachfrage freigeben, in `.claude/settings.local.json` (steht nicht im Repo). Alle Werkzeuge:
   ```json
   {"permissions": {"allow": ["mcp__ubuchhaltung"]}}
   ```
   Oder nur die reinen Prüfwerkzeuge (bei `sql` und `page` fragt der Client dann weiter nach):
   ```json
   {"permissions": {"allow": ["mcp__ubuchhaltung__info", "mcp__ubuchhaltung__schema", "mcp__ubuchhaltung__db_check"]}}
   ```

**Andere Systeme und Variablen:**

- Linux/macOS: in der Registrierung zusätzlich `--env UBUCHHALTUNG_MCP_USER=<Benutzer>` angeben und das Passwort mit dem
  Paket `keyring` ablegen (`keyring set uBuchhaltung-MCP <Benutzer>`). Als Notlösung geht `UBUCHHALTUNG_MCP_PASSWORD` im
  Klartext – nur zusammen mit `UBUCHHALTUNG_MCP_USER`.
- `UBUCHHALTUNG_MCP_URL` (Pflicht): Adresse der Instanz. `UBUCHHALTUNG_MCP_CAFILE`: PEM-Datei für HTTPS mit eigenem
  Zertifikat. `UBUCHHALTUNG_MCP_CREDENTIAL`: Zielname in der Anmeldeinformationsverwaltung bzw. keyring-Dienst (Standard
  `uBuchhaltung-MCP`). `UBUCHHALTUNG_MCP_TIMEOUT`: Sekunden je Anfrage (Standard 30). `UBUCHHALTUNG_MCP_ALLOW_HTTP`
  (`1`/`true`/`yes`/`ja`): Zugangsdaten bewusst über unverschlüsseltes `http://` an einen fremden Rechner senden.
- Meldet der Server „Eintrag in der Anmeldeinformationsverwaltung ist kein UTF-16-Text“, den Eintrag mit `cmdkey` neu anlegen.

## Umzug auf Version 2

Ab Version 2.0.0 gibt es nur noch die Ablage `data/users/<Name>/`; das Datenbankschema ändert sich dabei nicht. Der frühere
Einzelbenutzer-Betrieb (Daten direkt in `data/`, ohne Anmeldung) heißt jetzt „automatische Anmeldung“.

**Vorher eine Datensicherung anlegen:** Server bzw. Container stoppen und das ganze Datenverzeichnis kopieren (Docker: den
eingebundenen Ordner für `/app/data`, Unraid z. B. `/mnt/user/appdata/ubuchhaltung`).

**Was beim ersten Start automatisch passiert** (nur bei `"mode": "single"` oder einer `data/buch.db` ohne `config.json`):

- Der Inhalt von `data/` wandert nach `data/users/admin/` – außer `config.json`, `auth.db`, `users/`, `tests/` und
  `lost+found`. Nichts wird gelöscht oder überschrieben. Die gespeicherten Pfade von Rechnungsdokumenten und Belegen werden
  auf den neuen Ort angepasst.
- Der Benutzer `admin` wird angelegt und **automatisch angemeldet**; es gibt weiterhin keine Anmeldeseite. Ist der Name
  vergeben, heißt der Benutzer `admin2` usw.
- Ein früheres Demo-Verzeichnis (Marker `.demo`) wird zum Benutzer `demo`, ohne automatische Anmeldung.

Bestehende Mehrbenutzer-Installationen sind nicht betroffen.

**Erfolg erkennt man daran:** Das Log des ersten Starts nennt den Umzug (`'./data' → './data/users/admin'`), die Zahl der
angepassten Dateipfade und den automatisch angemeldeten Benutzer; `data/config.json` lautet
`{"mode": "multi", "auto_login": "admin"}`; direkt in `data/` liegen nur noch `config.json`, `auth.db` und `users/`. Beim
nächsten Start steht davon nichts mehr im Log.

**Bei Abbruch:** Der Umzug ist wiederaufnehmbar. `config.json` trägt bis zum Ende die Marke `migrating_to`; einfach neu
starten. Meldet der Start „… existiert bereits“, wurde nichts überschrieben: Im Ziel unter `data/users/<Name>/` liegt schon
etwas mit demselben Namen. Das muss von Hand geklärt werden (Eintrag umbenennen oder zusammenführen), danach neu starten.

**Zurück auf 1.x:** Server stoppen, den Inhalt von `data/users/admin/` zurück nach `data/` verschieben, `config.json` auf
`{"mode": "single"}` setzen und das alte Image bzw. die alte Version starten. Die in der Datenbank gespeicherten Pfade werden
dabei nicht zurückgeschrieben: Rechnungs-PDFs findet die alte Version über das Namensschema wieder, die Pfadangaben in der
Belegliste zeigen dann noch auf den neuen Ort. Soll später doch wieder Version 2 laufen, vorher das leere
`data/users/admin/` und `data/auth.db` entfernen – sonst ist der Name vergeben und der Umzug legt `admin2` an.

**Anmeldung nachträglich einschalten:** Unter *Stammdaten → Benutzer* die automatische Anmeldung ausschalten und dabei ein
Passwort setzen. Das Passwort des Benutzers `admin` kennt sonst niemand – es wird beim Umzug zufällig erzeugt. Wurde
`auto_login` von Hand aus `config.json` entfernt, ohne vorher ein Passwort zu setzen, den Eintrag `"auto_login": "admin"`
wieder hinzufügen und das Passwort dann in der Benutzerverwaltung setzen.

## Demo-Modus (ausprobieren)

`UBUCHHALTUNG_DEMO=1` startet uBuchhaltung mit erfundenen Beispieldaten, ohne Ersteinrichtung:

    docker run -p 2824:2824 -e UBUCHHALTUNG_DEMO=1 ghcr.io/nobsevomberg/ubuchhaltung:latest
    UBUCHHALTUNG_DEMO=1 python main.py        # Linux/macOS (Windows: $env:UBUCHHALTUNG_DEMO = "1")

- Die Demodaten gehören dem Benutzer `demo` und liegen **ausschließlich unter `data/users/demo/`**. Geladen werden sie nur,
  wenn dieses Verzeichnis fehlt oder leer ist **und** es noch keinen Benutzer `demo` gibt; sonst gibt es eine Warnung im Log
  und einen normalen Start. Andere Daten in `data/` werden nie angefasst. Ein Neustart mit demselben Verzeichnis befüllt
  nicht erneut (Marker `data/users/demo/.demo`).
- Solange die Variable gesetzt ist, arbeitet jeder Besucher ohne Anmeldung als `demo`. `demo` ist **kein Administrator**
  (keine Benutzerverwaltung, keine SQL-Konsole); andere Benutzer können sich so lange nicht anmelden. Die App nimmt keine
  Dateien an (Belege, Kontoauszüge, Logos, Importe) und zeigt auf jeder Seite den Hinweis „Demo-Modus – Beispieldaten, nicht
  für die echte Buchhaltung“.
- Ohne die Variable läuft die Instanz normal. `demo` ist dann ein gewöhnlicher Benutzer ohne bekanntes Passwort: Ein
  Administrator kann ihm in der Benutzerverwaltung eines setzen oder ihn löschen (sein Verzeichnis bleibt dabei liegen).
  Enthält `data/` nur die Demo und noch keinen Administrator, zeigt der Start ohne Variable die Ersteinrichtung.
- Inhalt: Musterfirma mit gut drei Jahren erfundener Buchhaltung (rund 300 Rechnungen mit Zahlungen, Angebote, Storno,
  offene und überfällige Posten, Ausgaben, Kasse, Anlagen mit AfA, Fahrtenbuch, Arbeitszeiten; das erste Jahr als
  Kleinunternehmerjahr), datiert relativ zum Startdatum. Das erste Laden dauert einige Sekunden; PDF und E-Rechnung gibt es
  nur für die Rechnungen der letzten 30 Tage („PDF + E-Rechnung“ erzeugt sie für ältere). Einzelheiten:
  `docs/regeln/werkzeuge.md`.
- **Nicht für öffentliches Hosting:** keine Anmeldung, und jeder Besucher darf die Demodaten ändern. Nur lokal oder im
  vertrauenswürdigen LAN verwenden.
