# uBuchhaltung im lokalen Netzwerk betreiben (Mehrbenutzer + HTTPS)

uBuchhaltung legt die Daten jedes Benutzers isoliert unter `data/users/<user>/`
ab. Bei der **Ersteinrichtung im Browser** wird der erste Administrator angelegt –
**mit Passwort** oder **ohne Anmeldung** (der Administrator wird dann automatisch
angemeldet, das ist der Betrieb für eine Person im eigenen Netz). Das Ergebnis
steht in `data/config.json` – keine Umgebungsvariable nötig.

Installationen aus Version 1.x (Daten direkt in `data/`) zieht der erste Start von
Version 2 automatisch um – siehe [Umzug auf Version 2](#umzug-auf-version-2).

## Konfiguration (`data/config.json`)

```json
{ "mode": "multi", "auto_login": "admin" }
```

- Solange es noch keinen Administrator gibt, fragt die **Ersteinrichtung** nach
  Benutzername und danach, ob mit oder ohne Anmeldung gestartet wird. Beides
  stellt `"mode": "multi"` ein; „ohne Anmeldung“ trägt zusätzlich `"auto_login"`
  mit dem Benutzernamen ein.
- Fehlt die Datei oder ist sie nicht lesbar, obwohl es schon einen Administrator
  gibt (z. B. nach einem Tippfehler beim Bearbeiten von Hand), zeigt die App einen
  Hinweis statt der Ersteinrichtung. Dann die Datei wiederherstellen – mit
  `{"mode": "multi"}` gilt die Anmeldung mit Passwort.
- `auto_login` (nur bei `"multi"`): dieser Administrator wird ohne Passwort
  angemeldet, die Anmeldeseite entfällt und andere Benutzer können sich so lange
  nicht anmelden. In der Benutzerverwaltung lässt sich die automatische Anmeldung
  ein- und ausschalten; beim Ausschalten wird ein Passwort gesetzt.
- Für ein **Headless-Setup** kann diese Datei vorab mit `{"mode":"multi"}`
  angelegt werden; die Ersteinrichtung (`/setup`) legt dann den Administrator an –
  mit oder ohne Anmeldung.
- `"mode": "multi"` markiert die Installation als eingerichtet. Die Ersteinrichtung
  trägt es ein; im Browser lässt es sich danach nicht mehr ändern. Jeder andere Wert
  gilt als „nicht eingerichtet“. Eine Ablage aus Version 1.x (Daten direkt in `data/`)
  wird beim Start umgezogen (siehe [Umzug auf Version 2](#umzug-auf-version-2)) und
  trägt danach `"multi"`.

## Netzwerk & HTTPS über Umgebungsvariablen

| Variable      | Default     | Bedeutung |
|---------------|-------------|-----------|
| `UBUCHHALTUNG_HOST` | `localhost` | Bind-Adresse. Für Netzzugriff `0.0.0.0`. |
| `UBUCHHALTUNG_PORT` | `8080`      | TCP-Port. |
| `UBUCHHALTUNG_CERT` | *(keine)*   | Pfad zur TLS-Zertifikatsdatei (PEM) ⇒ HTTPS. |
| `UBUCHHALTUNG_KEY`  | *(keine)*   | Pfad zum privaten TLS-Schlüssel (PEM). |
| `UBUCHHALTUNG_ALLOWED_HOSTS` | *(leer)* | Zusätzliche Hostnamen (Kommaliste), unter denen die App erreichbar sein darf – siehe „Schutz vor fremden Webseiten“. |

Sind `UBUCHHALTUNG_CERT` **und** `UBUCHHALTUNG_KEY` gesetzt, läuft der Server über HTTPS und
setzt das `Secure`-Flag der Session-Cookies.

## Schutz vor fremden Webseiten

- Schreibende Anfragen (POST) nimmt die App nur von ihren eigenen Seiten an
  (Prüfung von `Sec-Fetch-Site` bzw. `Origin` gegen `Host`). Löschen und Ändern
  laufen ausschließlich per POST.
- Ohne Login (Ersteinrichtung, automatische Anmeldung, Demo-Modus) antwortet die App nur unter
  IP-Adressen, `localhost`, Namen ohne Punkt, Namen mit den Endungen `.local`,
  `.lan`, `.home.arpa`, `.internal`, `.fritz.box`, `.localhost` und dem eigenen
  Rechnernamen. Das schützt vor DNS-Rebinding. Für andere Namen (z. B. eigene
  Domain am Reverse-Proxy, Tailscale-Name):
  ```bash
  UBUCHHALTUNG_ALLOWED_HOSTS=buch.example.org,server.tailnet.ts.net
  ```
- Reverse-Proxy: `Host` durchreichen oder `X-Forwarded-Host` setzen (nginx Proxy
  Manager, Caddy und Traefik tun das von sich aus); sonst den öffentlichen Namen
  in `UBUCHHALTUNG_ALLOWED_HOSTS` eintragen.

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
2. Im Browser öffnen: Die **Ersteinrichtung** legt das **Administrator-Konto** an –
   **mit Passwort** oder **ohne Anmeldung** (automatische Anmeldung, Betrieb für eine
   Person). Der Administrator legt unter **Stammdaten → Benutzer** weitere Nutzer an.
   Jeder Nutzer richtet beim ersten Login seine eigenen Firmen-/Kontaktdaten ein.

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
- Die Wiederherstellung in der App prüft das Archiv, bevor sie am Datenverzeichnis etwas
  ändert. Bleibt nach einem Absturz mitten im Wiederherstellen ein Ordner
  `backup/replaced_…` zurück, enthält er den Stand von davor – von dort zurückkopieren
  und den Ordner erst danach löschen.
- Ist ein Ordner im Datenverzeichnis nur ein Link auf einen anderen Ort (z. B. Belege auf
  einem NAS), ersetzt „vorher löschen“ den Link durch einen normalen Ordner; das Linkziel
  bleibt unberührt. Als eigenes Volume eingebundene Ordner bleiben eingebunden.
- Das Löschen eines Nutzers in der Benutzerverwaltung entfernt nur das Login;
  das zugehörige Datenverzeichnis bleibt erhalten und kann bei Bedarf manuell
  entfernt werden.
- Die SQL-Konsole (*Sonstiges*) steht nur Administratoren zur Verfügung und
  arbeitet immer nur auf der eigenen Datenbank des angemeldeten Nutzers. Wer sie
  braucht, bekommt in der Benutzerverwaltung das Administratorrecht.

## Docker (Unraid, Proxmox, NAS)

Zielplattformen sind Server, die ohnehin Docker anbieten. Das fertige Image
kommt aus der GitHub Container Registry – gebaut und veröffentlicht durch
`.github/workflows/docker-publish.yml`:

```
ghcr.io/nobsevomberg/ubuchhaltung:latest
```

| Tag | Inhalt |
|---|---|
| `:latest` | neuester Release (empfohlen) |
| `:1.6.16`, `:1.6`, `:1` | fester Release bzw. neuester innerhalb der Reihe – zum Festhalten oder Zurückgehen |
| `:edge` | aktueller Entwicklungsstand von `main`, ungetestet im Betrieb |

Releases entstehen automatisch: Kommt auf `main` eine neue Version aus
`version.py` an (noch kein Tag dafür), laufen die Tests, das Image wird gebaut
und danach Tag und GitHub-Release mit Release-Notizen angelegt. Änderungen ohne
neue Version (z. B. Doku) bauen nur `:edge`.

Standardport ist **2824** („BUCH" auf der Telefontastatur – von verbreiteten
Docker-Projekten nicht belegt). Alle Nutzdaten (auth.db, Nutzer-Verzeichnisse,
Datensicherungen) liegen unter `/app/data` – für ein Backup genügt weiterhin
dieses eine Verzeichnis.

- Beim ersten Aufruf erscheint die Ersteinrichtung (Administrator anlegen, mit
  Passwort oder ohne Anmeldung). Headless-Setup: im Daten-Verzeichnis vorab
  `config.json` mit `{"mode":"multi"}` anlegen. Wer von Version 1.x kommt: der erste
  Start zieht die bestehenden Daten um – siehe [Umzug auf Version 2](#umzug-auf-version-2).
- Der Container startet als root, übereignet das Daten-Volume einmalig dem
  unprivilegierten App-Benutzer (UID 1000) und gibt die Rechte dann ab –
  root-eigene appdata-Verzeichnisse (Unraid) funktionieren so ohne Handarbeit.
- Das Firmenlogo lädt man in den Stammdaten beim eigenen Kontakt über „Datei
  wählen“ hoch – der Dialog zeigt den eigenen Rechner, die Datei landet im
  Daten-Volume (`users/<Name>/logos/`). Ein von Hand eingetippter Pfad vom eigenen PC
  funktioniert im Container nicht; das Formular weist dann darauf hin.
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

Variante 1 – Community Applications (empfohlen): im Tab **Apps** nach
„uBuchhaltung" suchen und installieren. Port und appdata-Pfad
(`/mnt/user/appdata/ubuchhaltung`) sind vorbelegt.

Variante 2 – Template von Hand: `unraid-template.xml` aus dem Projektstamm nach
`/boot/config/plugins/dockerMan/templates-user/` kopieren, dann unter
**Docker → Add Container** das Template „uBuchhaltung" auswählen. Port und
appdata-Pfad (`/mnt/user/appdata/ubuchhaltung`) sind vorbelegt.

Variante 3 – manuell: **Docker → Add Container**, Repository
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

## Diagnose mit Claude Code (MCP, nur lesend)

Mit `tools/mcp_ubuchhaltung.py` kann Claude Code eine laufende Instanz (z. B. den
Docker-Container im LAN) untersuchen, ohne Browser. Der Server läuft **lokal auf dem
Entwicklungsrechner** – Claude Code startet ihn als stdio-MCP-Server – und fragt die
Instanz per HTTP ab wie ein Skript. Auf dem NAS bzw. im Container ist nichts zu
installieren; nötig sind nur Python (ab 3.10) und die Standardbibliothek.

**Was er kann** – fünf Werkzeuge:

| Werkzeug | Zweck |
|---|---|
| `info` | Adresse, Anmeldung und Version der Instanz |
| `sql` | genau ein `SELECT` in der Datenbank des angemeldeten Kontos |
| `db_check` | `quick_check` (mit `full`: `integrity_check`), Fremdschlüssel, Schema-Version, Journal-Modus, Tabellenzahl |
| `schema` | alle Tabellen mit Zeilenzahl; mit Tabellenname: `CREATE`-Anweisung, Spalten, Fremdschlüssel, Indizes |
| `page` | eine geprüfte Anzeigeseite der App per GET, als Text (z. B. `/about`, `/invoice?id=3`, `/users`) |

`sql` gibt zuerst die Spaltennamen als JSON-Array aus, dann jede Zeile als JSON-Array (`NULL`
als `null`, Zahlen als Zahlen); so bleiben Zeilenumbrüche und Semikolons in Werten
eindeutig. **Geldbeträge stehen als Ganzzahl in Minor Units** (Skalierung 4: `1190000` =
119,00 €). Gleich benannte Spalten (`a.ID`, `b.ID`) benennt SQLite um (`ID`, `ID:1`) – besser
mit `AS` unterscheiden. BLOB-Spalten lassen sich nur über `hex(x)` oder `length(x)` abfragen.

**Was er nicht kann – und seine Grenzen:**

- **SQL:** nur ein einzelnes `SELECT` ohne `;` (kein `INSERT`/`UPDATE`/`DELETE`, kein
  `PRAGMA` zum Setzen, keine zweite Anweisung). `pragma_<name>` als Tabellenfunktion ist nur
  für rein lesende Namen erlaubt (`_READ_PRAGMAS`, z. B. `table_info`, `foreign_key_check`,
  `quick_check`, `user_version`); `pragma_optimize` zum Beispiel würde schreiben. Gesperrt
  sind außerdem `WITH`/`RECURSIVE`, `zeroblob`, `randomblob`, Blockkommentare außer am Anfang
  und Abfragen über 20 000 Zeichen.
- **Seiten:** nur aus einer Positivliste geprüfter Anzeigeseiten mit den je Seite erlaubten
  Query-Parametern (`_PAGE_ALLOWLIST` in `tools/mcp_ubuchhaltung.py`). Manche GET-Routen der
  App ändern Daten oder erzeugen PDFs, und manche Seiten schreiben je nach Parameter.
  Aktionen, PDF-Erzeugung, `/login` und `/logout` stehen nicht in der Liste.
- **Sonst:** Außer der Anmeldung und der SQL-Konsole (`/execute_sql`) sendet er keine POSTs.
  Antworten der Instanz begrenzt er auf 16 MiB und auf die doppelte Zeitgrenze je Anfrage.
  Das Passwort steht nie in Ausgaben oder Logs.
- **Restrisiko:** Die SQL-Konsole der Instanz hat kein Zeitlimit. Die Sperren fangen die
  üblichen Endlosabfragen ab, aber nicht jede aufwendige (etwa Kreuzprodukte großer
  Tabellen). Solche Abfragen belasten die Instanz und blockieren Schreibzugriffe, solange
  sie laufen.

**Voraussetzung:** ein Konto mit Administratorrechten – die SQL-Konsole steht nur
Administratoren zur Verfügung und sieht ausschließlich die Datenbank des angemeldeten
Kontos. Bei automatischer Anmeldung braucht der Server keine Zugangsdaten; dort gilt die
Hostnamen-Prüfung aus „Schutz vor fremden Webseiten“ (bei einem Namen außerhalb der
dort genannten Endungen `UBUCHHALTUNG_ALLOWED_HOSTS` auf der Instanz setzen).

**Einrichtung unter Windows (PowerShell):**

1. Zugangsdaten ablegen. `cmdkey` fragt das Passwort ab; es liegt danach verschlüsselt in
   der Windows-Anmeldeinformationsverwaltung, nicht im Projektordner:
   ```powershell
   cmdkey /generic:uBuchhaltung-MCP /user:<Benutzer> /pass
   ```
2. Server registrieren. Der lokale Scope hält die Adresse außerhalb des Repos. **Empfohlen
   ist HTTPS** (`https://…`, bei eigenem Zertifikat zusätzlich
   `--env UBUCHHALTUNG_MCP_CAFILE=<Pfad>`). Über `http://` sendet der Server Benutzername und
   Passwort nur an den eigenen Rechner (`localhost`, `127.0.0.1`, `::1`). Im reinen LAN ohne
   HTTPS muss die Ausnahme bewusst gesetzt werden; Passwort und Sitzung gehen dann
   unverschlüsselt durchs Netz:
   ```powershell
   claude mcp add --scope local ubuchhaltung --env UBUCHHALTUNG_MCP_URL=http://nas.local:2824 --env UBUCHHALTUNG_MCP_ALLOW_HTTP=1 -- python <Projektordner>\tools\mcp_ubuchhaltung.py
   ```
   Bei automatischer Anmeldung der Instanz werden keine Zugangsdaten gesendet; dort braucht
   es die Ausnahme nicht.
3. Optional ohne Nachfrage freigeben, in `.claude/settings.local.json` (steht nicht im Repo).
   Alle Werkzeuge:
   ```json
   {"permissions": {"allow": ["mcp__ubuchhaltung"]}}
   ```
   Oder nur einzelne, z. B. die reinen Prüfwerkzeuge – bei `sql` und `page` fragt Claude Code
   dann weiter nach:
   ```json
   {"permissions": {"allow": ["mcp__ubuchhaltung__info", "mcp__ubuchhaltung__schema", "mcp__ubuchhaltung__db_check"]}}
   ```

**Andere Systeme und Sonderfälle:**

- Linux/macOS: in der Registrierung zusätzlich `--env UBUCHHALTUNG_MCP_USER=<Benutzer>`
  angeben und das Passwort mit dem Paket `keyring` ablegen
  (`keyring set uBuchhaltung-MCP <Benutzer>`). Als Notlösung geht
  `UBUCHHALTUNG_MCP_PASSWORD` im Klartext – nur zusammen mit `UBUCHHALTUNG_MCP_USER`.
- HTTPS mit eigenem Zertifikat: `UBUCHHALTUNG_MCP_CAFILE` auf die Zertifikatsdatei (PEM)
  zeigen lassen, in der Registrierung zusätzlich `--env UBUCHHALTUNG_MCP_CAFILE=<Pfad>`.
- Weitere Variablen: `UBUCHHALTUNG_MCP_CREDENTIAL` (Zielname in der Anmeldeinformationsverwaltung bzw. keyring-Dienst,
  Standard `uBuchhaltung-MCP`), `UBUCHHALTUNG_MCP_TIMEOUT` (Sekunden je Anfrage, Standard 30)
  und `UBUCHHALTUNG_MCP_ALLOW_HTTP` (`1`/`true`/`yes`/`ja`: Zugangsdaten bewusst über
  unverschlüsseltes `http://` an einen fremden Rechner senden).
- Meldet der Server „Eintrag in der Anmeldeinformationsverwaltung ist kein UTF-16-Text“, den Eintrag mit `cmdkey` neu
  anlegen (Schritt 1).

## Umzug auf Version 2

**Was sich ändert:** Ab Version 2.0.0 gibt es nur noch die Ablage `data/users/<Name>/`.
Das Datenbankschema selbst ändert sich nicht. Der frühere
Einzelbenutzer-Betrieb (Daten direkt in `data/`, ohne Anmeldung) heißt jetzt
„automatische Anmeldung“: Ein Administrator wird ohne Passwort angemeldet
(`auto_login` in `data/config.json`, siehe oben).

**Was beim ersten Start von Version 2 automatisch passiert** (nur bei einer
Einzelbenutzer-Installation, also `"mode": "single"` oder einer `data/buch.db`
ohne `config.json`):

- Der Inhalt von `data/` wandert nach `data/users/admin/` – außer `config.json`,
  `auth.db`, `users/` und `tests/`. Nichts wird gelöscht oder überschrieben.
- Die gespeicherten Pfade von Rechnungsdokumenten (PDF, XML) und Belegen werden auf den
  neuen Ort angepasst.
- Der Benutzer `admin` wird angelegt und **automatisch angemeldet**. An der Bedienung
  ändert sich nichts: Es gibt weiterhin keine Anmeldeseite. (Ist der Name `admin` schon
  vergeben, heißt der Benutzer `admin2` usw.)
- Ein früheres Demo-Verzeichnis (Marker `.demo`) wird zum Benutzer `demo`, ohne
  automatische Anmeldung (siehe [Demo-Modus](#demo-modus-ausprobieren)).

Bestehende **Mehrbenutzer-Installationen** sind nicht betroffen.

**Vorher eine Datensicherung anlegen:** Server bzw. Container stoppen und das ganze
Datenverzeichnis kopieren (Docker: den eingebundenen Ordner für `/app/data`, Unraid
z. B. `/mnt/user/appdata/ubuchhaltung`).

**Woran man den erfolgreichen Umzug erkennt:**

- Das Log des ersten Starts nennt den Umzug (`'./data' → './data/users/admin'`), die
  Zahl der angepassten Dateipfade und den automatisch angemeldeten Benutzer.
- `data/config.json` lautet `{"mode": "multi", "auto_login": "admin"}`.
- Direkt in `data/` liegen nur noch `config.json`, `auth.db` und `users/`.
- Beim nächsten Start steht davon nichts mehr im Log: Der Umzug läuft nur einmal.

**Bei Abbruch:** Der Umzug ist wiederaufnehmbar. `config.json` trägt bis zum Ende die
Marke `migrating_to`; einfach neu starten. Meldet der Start „… existiert bereits“, wurde
nichts überschrieben: Im Ziel unter `data/users/<Name>/` liegt schon etwas mit
demselben Namen. Das muss von Hand geklärt werden (vorhandenen Eintrag umbenennen oder
zusammenführen), danach neu starten.

**Zurück auf 1.x:** Server stoppen, den Inhalt von `data/users/admin/` zurück nach
`data/` verschieben, `config.json` auf `{"mode": "single"}` setzen und das alte Image
bzw. die alte Version starten. Die in der Datenbank gespeicherten Pfade werden dabei
nicht zurückgeschrieben: Rechnungs-PDFs findet die alte Version über das Namensschema
wieder, die Pfadangaben in der Belegliste zeigen dann noch auf den neuen Ort. Soll
später doch wieder Version 2 laufen, vorher das leere `data/users/admin/` und
`data/auth.db` (sie enthält nur den beim Umzug angelegten `admin`) entfernen – sonst
ist der Name vergeben und der Umzug legt den Benutzer `admin2` an.

**Anmeldung nachträglich einschalten:** Unter **Stammdaten → Benutzer** die
automatische Anmeldung ausschalten und dabei ein Passwort setzen. Das Passwort des
Benutzers `admin` kennt sonst niemand – es wird beim Umzug zufällig erzeugt. Wurde
`auto_login` von Hand aus `config.json` entfernt, ohne vorher ein Passwort zu setzen,
den Eintrag `"auto_login": "admin"` wieder hinzufügen und es dann in der
Benutzerverwaltung setzen.

## Demo-Modus (ausprobieren)

`UBUCHHALTUNG_DEMO=1` startet uBuchhaltung mit erfundenen Beispieldaten – ohne
Ersteinrichtung:

    docker run -p 2824:2824 -e UBUCHHALTUNG_DEMO=1 ghcr.io/nobsevomberg/ubuchhaltung:latest
    UBUCHHALTUNG_DEMO=1 python main.py        # Linux/macOS (Windows: $env:UBUCHHALTUNG_DEMO = "1")

- Die Demodaten gehören dem Benutzer `demo` und liegen **ausschließlich unter
  `data/users/demo/`**. Geladen werden sie nur, wenn dieses Verzeichnis fehlt oder leer
  ist **und** es noch keinen Benutzer `demo` gibt; sonst gibt es eine Warnung im Log und
  einen normalen Start. Andere Daten in `data/` werden nie angefasst – echte Bücher
  und Demo können nebeneinander liegen.
- Solange die Variable gesetzt ist, arbeitet jeder Besucher ohne Anmeldung als `demo`.
  `demo` ist **kein Administrator**: keine Benutzerverwaltung, keine SQL-Konsole.
  Andere Benutzer können sich so lange nicht anmelden (auch ein Administrator mit
  automatischer Anmeldung nicht).
- Ohne die Variable läuft die Instanz normal. `demo` ist dann ein gewöhnlicher
  Benutzer ohne bekanntes Passwort: Ein Administrator kann ihm in der Benutzerverwaltung
  eines setzen oder ihn löschen (sein Verzeichnis `data/users/demo/` bleibt dabei
  liegen). Enthält `data/` nur die Demo und noch keinen Administrator, zeigt der Start
  ohne Variable die Ersteinrichtung.
- Inhalt: Musterfirma mit gut zwei Jahren erfundener Buchhaltung (Rechnungen mit Zahlungen,
  offene und überfällige Posten, Ausgaben, Splits, Anlagen, Fahrtenbuch, Arbeitszeiten);
  alle Daten sind relativ zum Startdatum datiert (`demo_seed.py`).
- Ein Neustart mit demselben Verzeichnis befüllt nicht erneut (Marker
  `data/users/demo/.demo`).
- Auf jeder Seite erscheint der Hinweis „Demo-Modus – Beispieldaten, nicht für die
  echte Buchhaltung“.
- **Nicht für öffentliches Hosting:** keine Anmeldung, und jeder Besucher darf die
  Demodaten ändern. Nur lokal oder im vertrauenswürdigen LAN verwenden.

## Schema-Migrationen

Bestehende Nutzer-DBs werden beim Start automatisch an die aktuelle
Schema-Version angeglichen (neue Spalten werden nachgezogen; `PRAGMA
user_version` spiegelt die `SCHEMA_VERSION` aus `version.py`). Es ist also kein
manuelles „DB löschen & neu" mehr nötig.
