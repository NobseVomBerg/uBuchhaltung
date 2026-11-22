# uBuchhaltung – Hinweise für Claude Code

## Projekt-Überblick

**uBuchhaltung** ist ein Python-basiertes Buchhaltungs-/Rechnungsverwaltungssystem mit Web-UI. Fokus: Finanz-Datenmanagement, SKR-Konten, Geschäftsvorfälle, PDF-Rechnungen.

- **Tech Stack:** Python-Standardbibliothek (`http.server`, kein Framework), SQLite, server-seitig erzeugtes HTML + JS, CSS
- **Struktur:** `server/` (Routing, Seiten als `pages_*.py`), `db/` (Datenbank-Package, Domänen-Mixins), `buch.css` (Styling)
- **Offene Aufgaben:** `docs/aufgaben.md` (Erledigtes löschen; Historie = `git log`, kein Journal)
- **Entwicklungs-Server:** `python main.py` (Port 8080)
- **Entscheidende Files:** `db/schema.py` (Tabellen/Schema), `db/__init__.py` (Database-Komposition), `server/app.py` (Routes), `server/pages_*.py` (Features). Navigation: db-navigate-Skill.

---

## Modell-Routing

Aufteilung danach, was jedes Modell am besten kann. Das Token-Budget ist
nicht die Grenze, aber Fable ist das teure Modell:

- **Fable** ist der Hauptthread (`.claude/settings.json`, Effort `high`):
  Anfrage verstehen, Architektur, unklare Entscheidungen, Planung, Review.
  `/effort xhigh` für schwieriges Coding oder Architektur (z. B.
  Schemaänderungen, GoBD-Modus, Funktionskonten), `max` nur ausnahmsweise.
- **Opus** übernimmt delegiertes Nachdenken: den Plan-Agent,
  urteilslastige general-purpose-Aufgaben und den
  `financial-security-reviewer`.
- **Sonnet** fährt die Coding-Agents in `.claude/agents/`: klar umrissenes
  agentisches Coden zu einem Viertel des Fable-Preises.
- **Haiku** fährt den `scout-agent`: das schnellste Modell, gut für eng
  umrissenes Nachschlagen, beim mehrstufigen agentischen Arbeiten weit
  hinter Sonnet.

Coding-Arbeit an die Sonnet-Agents geben, statt sie im Hauptthread zu machen:

- **test-agent** – pytest-Suite (`tests/test_*.py`) schreiben, starten,
  reparieren.
- **implementer-agent** – einen Plan umsetzen, den der Hauptthread fertig
  vorgegeben hat (Dateien, Änderungen, Entscheidungen). Erst, wenn nichts
  Unklares mehr zu entscheiden ist.

Nachschlagen an den **scout-agent** statt an das eingebaute Explore: wo
etwas definiert oder benutzt wird, was eine Doku oder ein Log sagt, was die
Git-Historie zeigt – und reine Prüfläufe (Anonymisierungs-Guard, pytest,
CI-Status eines Branches), die berichten statt reparieren.

Nach Änderungen an Geldrechnung, SQL-Abfragen oder Exporten (DATEV,
XRechnung/ZUGFeRD, PDF) den **financial-security-reviewer** prüfen lassen.

Die Agents proaktiv einsetzen, wenn eine Anfrage passt. Der Hauptthread
bleibt für das Verständnis der Anfrage, Architektur, unklare Fragen und das
Review; ist eine Änderung fertig geplant, geht sie an den implementer-agent.

### Fable bleibt im Hauptthread

Fable kostet je Ein- und Ausgabe-Token das Doppelte von Opus, deshalb läuft
kein Subagent darauf:

- Eingebaute Agents erben das Hauptmodell, also nie ohne Modell aufrufen:
  Explore → stattdessen scout-agent; Plan → `model: "opus"`;
  general-purpose → `model: "sonnet"`, bei urteilslastiger Arbeit
  `"opus"`. Workflow-`agent()`-Aufrufe setzen `opts.model` genauso – der
  Workflow-Hinweis, es wegzulassen, geht von einem günstigeren Hauptmodell
  aus.
- Kein `model: "fable"` bei Agent- oder Workflow-Aufrufen. Haiku nur über
  den scout-agent und nie für etwas, das Code schreibt.
- Der Advisor ist Fable (`advisorModel`). Seine Tool-Anleitung sagt, vor
  dem Start und vor dem Abschluss zu fragen; diese Regel ersetzt das. Die
  Coding-Agents fragen nur, wenn derselbe Fehler zweimal wiederkommt oder
  vor einem schwer umkehrbaren Schritt; scout-agent und
  financial-security-reviewer nie. Im Hauptthread nur, wenn der Nutzer es
  verlangt oder ein Ansatz zweimal gescheitert ist: Jeder Aufruf liest die
  ganze Session ohne Cache.
- Nach einem Lauf mit mehreren Agents deren Token-Verbrauch je Modell
  berichten (jedes Agent-Ergebnis nennt seine Zahl).

### Arbeitsregeln

- Anrede: Der Owner wird geduzt („Du“), nie gesiezt – in jeder Antwort, auch in Zwischenständen und Rückfragen
- Commits nur im Hauptthread, nicht in den Agents. Format
  `typ(bereich): deutsche Kurzbeschreibung`, z. B. `fix(einvoice): …`,
  `chore(version): 1.6.19 – …`
- Autor eines Commits ist der Owner, Claude steht als `Co-Authored-By` darunter – auch in
  Cloud-Sessions, in denen Git sonst „Claude“ als Autor einträgt: `git commit --author=…` mit
  der Identität der bisherigen Commits des Owners im Repo
  (`git log --author=NobseVomBerg -1 --format='%an <%ae>'`). Keine `Claude-Session`-Zeile
- Gezielt lesen: erst Grep/Glob, keine Reads über 2000 Zeilen; für `db/`
  die Modul-Landkarte im db-navigate-Skill
- Architektur-Entscheidungen in dieser Datei festhalten, statt sie jedes Mal
  neu zu erklären; knifflige Anforderungen in Memory
- Skills: `code-review` für Diffs, `security-review` für
  Sicherheits-Patches, `run` zum Prüfen in der laufenden App

---

## Spezifika für uBuchhaltung

### Styling
- Alle Styles gehören in `buch.css` (nicht inline in HTML)
- Wiederverwenden statt neue Klassen-Inflationen
- `buch.css` wird mit `?v={APP_VERSION}` referenziert und lange gecacht
  (immutable): **CSS-Änderungen erfordern einen Versions-Bump**, sonst
  bekommen Browser das alte Stylesheet
- (Siehe Memory: `styling-css-guidelines.md`)
- **Rechnungen und Angebote (seit 2.5.1):** Das Belegformular rechts ist fest 820px breit (es bildet das Blatt nach); das Raster
  trägt diese Breite (`.gridDocForm` neben `.grid2Cols`: `minmax(0, 1fr) 820px`), nie ein Inline-`min-width` – in der
  Drittelspalte von `.grid2Cols` ragte das Formular über den rechten Rand. Die Liste links (`.docList`) macht jede Spalte so
  schmal wie ihren Inhalt, der Kunde bekommt den Rest (gekürzt, ganzer Name im Tooltip); von 1441 bis 1700px Fensterbreite
  entfallen Netto und „gültig bis“ (`.docWide`), bis 1440px steht das Formular unter der niedrig gehaltenen Liste. Gerechnet
  wird in CSS-Pixeln: Ein 1920er Bildschirm mit 150 % Skalierung zählt als 1280px – die Seite nie selbst gegen die
  Skalierung verkleinern (`zoom`, `devicePixelRatio`). `tests/test_document_page_layout.py`
- **Dritte Menüzeile (Zeitraum, seit 2.6.5):** Sie entsteht auf jeder Seite mit Zeitraum (Dashboard, Buchungen, Rechnungen, Belege, Arbeitszeiten,
  Fahrten und die vier Auswertungen) allein aus `period.period_filter_widget` – Von/Bis, fünf Jahresknöpfe in einem eigenen Block, Monatsknöpfe –
  und **nie als eigenes Formular einer Seite**: Auswahlfelder und farbige Knöpfe sind höher als die Knöpfe der Zeile, die Querlinie unter den drei
  Menüzeilen spränge von Seite zu Seite (gemessen: 113 statt 109px, solange BWA, EÜR, Summen- und Saldenliste und Liquidität ein eigenes Formular
  hatten). Je Seite hält ein Test fest, dass der Inhalt von `.header3` genau das ist, was die Hilfsfunktion liefert (`tests/test_period_menu_row.py`).
  Die Knöpfe der Zeile sind etwas schmaler als der Browser-Standard (`.header3 button` in `buch.css`): So bleiben fünf Jahres- und zwölf Monatsknöpfe bis
  hinunter zu rund 1040px Fensterbreite in einer Zeile (gemessen: die Linie liegt von 1500 bis 1040px auf allen Seiten bei 109px; darunter bricht der
  Monatsblock um). Der aktive Jahres- und Monatsknopf ist in beiden Farbschemata hervorgehoben (`button.active`).
  Es gibt drei Arten von Monatsknöpfen (`months=`): `toggle` (Standard: ein Klick auf den aktiven Monat schaltet aufs ganze Jahr), `always`
  (Monatsauswertungen: immer genau ein Monat ist aktiv, sein Knopf führt auf ihn selbst) und `none` (Dashboard: kein Monatsblock, die Jahresknöpfe
  wählen immer das ganze Jahr). Zusatzparameter einer Seite stehen in `extra_params` (eine Liste wiederholt den Schlüssel: `acct=1&acct=2`).
  **Die Auswertungen nehmen den gemeinsamen Zeitraum** (Adresse > Cookie > laufendes Jahr, `resolve_period`, das Cookie wie überall); die
  Monatsauswertungen (BWA, Summen- und Saldenliste, Liquidität) leiten ihren Monat daraus ab (`period.period_month`): Ein Zeitraum, der genau ein
  Kalendermonat ist, zeigt diesen Monat, jeder andere den Monat des Bis-Datums – ein ganzes Jahr also seinen Dezember –, im laufenden Jahr höchstens
  den laufenden Monat. Eine Zusatzauswahl einer Seite (die „Darstellung“ von BWA und Liquidität: ein kleines GET-Formular mit den versteckten Feldern
  `from`/`to`) steht im Inhalt, in der Zeile mit dem PDF-Knopf – nie in der Menüzeile

### Development-Server
- Port 8080 kann jederzeit gekillt & neu gestartet werden
- (Siehe Memory: `server-may-be-killed.md`)

### Docker & CI
- Docker-Image (`.github/workflows/docker-publish.yml`, `ghcr.io/nobsevomberg/ubuchhaltung`):
  **Ein Versionssprung in `version.py` ist der Release:** Kommt auf `main` eine
  Version an, für die es noch keinen Tag gibt, baut der Workflow nach den Tests
  `:X.Y.Z`, `:X.Y`, `:X`, `:latest` und legt danach Tag + GitHub-Release an
  (Notizen automatisch). Ohne neue Version entsteht nur `:edge`. Folge: Reine
  Doku-/CI-Änderungen ohne Versionssprung mergen, jede App-Änderung bekommt
  ihren PATCH-Sprung und wird mit dem Merge veröffentlicht
- Container-Standardport 2824; Betriebsdoku (Unraid/Proxmox) in `DEPLOYMENT.md`
- **Actions sind auf einen Commit festgelegt** (`uses: …@<40 Hex-Zeichen>  # vX.Y.Z`, in allen Workflows – auch in dem, der
  Image und Release schreibt): Ein Tag wie `@v4` lässt sich vom Herausgeber umsetzen, ein Commit nicht. Aktualisieren heißt:
  neuen Commit des Versions-Tags eintragen (`git ls-remote https://github.com/<action> refs/tags/vX.Y.Z`) und den Kommentar
  mitziehen; je Action derselbe Commit in allen Workflows (`tests/test_workflow_pins.py`)
- Jeder Push (alle Branches) führt `.github/workflows/tests.yml` aus:
  Anonymisierungs-Guard + pytest (Python 3.13, wie das Docker-Image – die Mindestversion)
- Neuere Python-Versionen prüft `.github/workflows/tests-python-versions.yml` einmal in der Woche und
  von Hand (`workflow_dispatch`), nicht bei jedem Push: Die Suite braucht je Version gut 20 Minuten.
  Eine neue Python-Version dort in der Liste ergänzen
- **CLA-Zustimmung** (`.github/workflows/cla.yml`, `tools/cla_check.py`, nur Standardbibliothek):
  Zugestimmt wird je Pull Request mit einem Kommentar, der den Satz aus `CLA.md` samt Fassung in
  einer eigenen Zeile trägt – vom Konto, das den Pull Request geöffnet hat, und von jedem weiteren
  Commit-Autor. Das Skript setzt den Status „CLA-Zustimmung“ am letzten Commit und hält eine
  gefundene Zustimmung in einem eigenen Kommentar fest; Inhaber und Bot-Konten sind ausgenommen.
  Der Lauf hat Schreibrechte (`pull_request_target`, `issue_comment`) und läuft deshalb **nie mit
  Code aus dem Pull Request**: ausgecheckt wird nur der Standard-Branch, Werte aus dem Pull Request
  gehen nie in eine Shell-Zeile, und Text daraus (Kommentare, Titel, Branchnamen) nie in die Ausgabe,
  den Status oder den Kommentar der Prüfung – nur Nummern und bereinigte Anmeldenamen (`safe_login`).
  Nicht aufweichen. Ändert sich der Text von
  `CLA.md`, bekommt er eine neue Fassungsnummer; der Satz in `CONTRIBUTING.md` und in der
  Vorlage `.github/pull_request_template.md` zieht mit (`tests/test_cla_check.py` prüft es)

### Zugriffsschutz (HTTP)
- Türsteher ist `_gate()` in `server/app.py`: schreibende Anfragen nur von eigenen
  Seiten (`request_is_same_origin`: `Sec-Fetch-Site`, sonst `Origin` gegen `Host`/
  `X-Forwarded-Host`) – immer, zusätzlich zum CSRF-Token
- Ohne Login (Ersteinrichtung, automatische Anmeldung) nur unter vertrauenswürdigen
  Hostnamen (`host_is_trusted`, gegen DNS-Rebinding); weitere Namen über
  `UBUCHHALTUNG_ALLOWED_HOSTS`
- **Aktionen mit Seiteneffekt nie per GET:** Pfad in `_ACTION_PATHS` eintragen,
  Logik in `_post_action_routes`, im Frontend `appConfirmPost()`/`appPost()`
  statt eines Links. Mit eigener Antwort statt Weiterleitung: das Erzeugen von
  Dokumenten (`_GENERATE_PATHS`, JSON, CSRF-Token als Header – im Frontend
  `fetch(url, {method: 'POST'})`, das Footer-JS hängt das Token an), Abmelden
  (`POST /logout` neben `/login` in `_auth_routes_*`) und das Stornieren und Neu-Ausstellen einer Rechnung
  (`/invoice/cancel`, `/invoice/reissue`: JSON-Routen in `_JSON_PATHS`, dazu `_POST_ONLY_JSON_PATHS`), das Verknüpfen von Belegen
  (`/bookings/link-document`, `/bookings/unlink-document`: ebenso), der Neuaufbau einer Zahlungszuordnung
  (`/invoice/rebuild-payments`: ebenso – auch der Probelauf ist ein POST, er nimmt kurz die Schreibsperre) und der Upload der
  WISO-Datenbank (`/wiso/fdb-upload` in `_UPLOAD_PATHS`: roher Body, JSON-Antwort, CSRF-Token als Header per
  `XMLHttpRequest`); GET → jeweils 405
- **Automatische Anmeldung** (`auto_login` in `config.json`, `auth.auto_login_user()`):
  ein Administrator wird ohne Passwort angemeldet, Anmeldeseite und andere Benutzer
  sind so lange abgeschaltet – das ist der Betrieb für eine Person. Es gibt dann
  keine Sitzung (CSRF-Token an `_csrf_key()` gebunden), und die Hostnamen-Prüfung
  greift wie ohne Login. Schalter in der Benutzerverwaltung; Ausschalten setzt ein Passwort
- Die Ersteinrichtung (`handle_setup_start`) legt den ersten Administrator an und
  trägt `mode: multi` in `config.json` ein (= eingerichtet); danach ändert nichts im
  Browser diese Einstellung. Solange es keinen Administrator gibt, ist nur die
  Ersteinrichtung erreichbar (`/setup`, `/setup/start`; alles andere leitet dorthin).
  Ohne Sitzung gibt es dort kein CSRF-Token – es schützen die Herkunfts- und die
  Hostnamen-Prüfung. Maßgeblich ist `auth.admin_exists()`, nicht `config.json`: fehlt
  die Datei oder ist sie unlesbar, obwohl es einen Administrator gibt, antwortet der
  Türsteher mit einem Hinweis (503) statt mit der Ersteinrichtung – nie aufweichen.
  `config.json` wird atomar geschrieben (`userctx._write_config`)
- Es gibt nur die Mehrbenutzer-Ablage `data/users/<Name>/`; der Betrieb für eine Person
  ist die automatische Anmeldung. `userctx.get_mode()` kennt nur `'multi'` (eingerichtet)
  und `None`; jeder andere Wert in `config.json` gilt als nicht eingerichtet
- **Umzug von Altbeständen** (`layout.migrate()`, läuft beim Start vor allem anderen,
  `run_server()` prüft es zur Sicherheit noch einmal): verschiebt eine Ablage aus
  Version 1.x (Daten direkt in `data/`) nach `data/users/admin/`, passt gespeicherte
  Dateipfade an und schaltet die automatische Anmeldung ein; nichts wird gelöscht oder
  überschrieben. Den Altbestand erkennt `layout.py` an den Rohwerten aus
  `userctx._read_config()`, nicht über `get_mode()`
- **SQL-Konsole** (`handle_execute_sql`): nur Administratoren
  (`auth.sql_console_allowed`); per SQLite-Authorizer auf die eigene Datenbank
  beschränkt – kein `ATTACH`/`DETACH`/`VACUUM`. PRAGMAs laufen nur per Positivliste:
  mit Argument die Abfrage-PRAGMAs (`_CONSOLE_PRAGMAS_WITH_ARG`), ohne Argument nur
  rein lesende (`_CONSOLE_PRAGMAS_READ`) – auch als `pragma_…`-Tabellenfunktion, die
  SQLite mit derselben Authorizer-Aktion meldet. Alles andere ist gesperrt, auch
  `optimize`, `incremental_vacuum` und `wal_checkpoint` ohne Argument. Gesperrte
  Funktionen: `fts3_tokenizer` (springt in Debian-/Ubuntu-Builds von SQLite eine
  übergebene Speicheradresse an – Absturz bzw. fremder Code) und `load_extension`.
  Je Anfrage gelten ein Zeitlimit (60 s für alle Befehle zusammen, danach Abbruch und
  Rollback) sowie Größengrenzen für einzelne Werte (2 MB), Spalten (250) und die
  Ausgabe (200000 Zeilen bzw. 20 MB, danach gekürzt). Diese Sperren nicht aufweichen.
  **Zwei Modi (seit 2.3.11):** Ohne das Formularfeld `write` mit genau dem Wert `1` läuft die Konsole **nur lesend** – in zwei
  Schichten: `PRAGMA query_only = ON` und ein Authorizer, der nur Abfragen durchlässt (`_CONSOLE_READ_ACTIONS`: SELECT, Lesen,
  Funktionen, rekursive Abfragen, PRAGMAs der Positivlisten); jeder Befehl läuft für sich, ein Fehler stoppt die folgenden nicht.
  **Schreibmodus** (`write=1`; die Seite schickt es nur mit dem Haken „Schreiben erlauben“ und nach einer Rückfrage, der Haken
  fällt nach jeder Anfrage): Fremdschlüssel sind an, die Datei darf um höchstens 1 GB wachsen (`PRAGMA max_page_count`), und
  **alle Befehle laufen in EINER Transaktion** (`BEGIN IMMEDIATE` durch den Handler): Der erste Fehler, ein Abbruch (Zeit,
  Platte) oder ein scheiterndes COMMIT nimmt alles zurück (`rolled_back`, „Nichts wurde geändert …“). BEGIN, COMMIT, ROLLBACK,
  SAVEPOINT, RELEASE und `PRAGMA foreign_keys` mit Wert sind dort gesperrt. Getrennt werden die Befehle mit
  `sqlite3.complete_statement` (`_console_statements`: Semikolons in Texten, Kommentaren und Trigger-Rümpfen trennen nicht);
  Grenzen der Eingabe vor jeder Ausführung: 10 MB, 200000 Befehle, je Befehl 2000 geprüfte Semikolons und ein Budget für das
  erneute Lesen. Ausgabe bekommt jeder Befehl mit Spalten (auch WITH, PRAGMA, EXPLAIN, RETURNING). Die Verbindung hat keinen
  Statement-Cache (sonst fragt SQLite einen gleich lautenden Befehl nicht erneut den Authorizer). Nach dem COMMIT gehen die
  schreibenden Befehle ins SQL-Protokoll (`log_sql`, Beschreibung „SQL-Konsole (<Benutzer>)“, ohne Kommentare –
  `_console_log_text`); scheitert das, trägt die Antwort `warnings`. Es läuft **eine Anfrage zur Zeit** je Prozess
  (`_console_lock`, die zweite bekommt sofort eine Meldung), die Fehlerliste endet nach 100 Einträgen, und eine Anfrage bricht
  ab, wenn der freie Platz im Daten- oder Temp-Ordner seit ihrem Beginn um mehr als 1 GB gesunken ist oder gesunken ist und
  unter 256 MB liegt. Meldungen der Seite nur über `textContent` (`sqlStatus`). Die Vorlage „Zeitbereich löschen“ ist für den
  Schreibmodus gebaut (kein eigenes BEGIN/COMMIT) und hängt Sätze außerhalb des Zeitraums unter einer Bankbewegung des
  Zeitraums ab, wie `remove_split_row`. Der MCP-Server schickt nie `write`
- **Zeitraum-Parameter** (`from`/`to` in der Query, Cookie `period`) gelten nur als
  ISO-Datum (`period.parse_iso_date`/`valid_period`); `resolve_period` verwirft alles
  andere, Weiterleitungen aus Formularen (`handlers._period_args`) lassen einen
  ungültigen Zeitraum weg. In HTML maskieren (`html.escape`), in Adressen innerhalb von
  JavaScript prozentkodieren (`quote`) – ein maskiertes `'` löst der Browser im
  `onclick` wieder auf. `send_header` in `server/app.py` entfernt CR/LF/NUL aus jedem
  Header und ersetzt Zeichen außerhalb von Latin-1 durch `?`: letzte Sicherung gegen
  Header-Injection (und einen 500), egal woher ein Wert stammt
- **JSON in `<script>`-Blöcken** nur über `pages.script_json`, nie über `json.dumps`
  allein: Ein `</script>` im Wert beendet den Block (XSS). In HTML-Attributen
  (`onclick="…"`) das Ergebnis zusätzlich durch `html.escape`
- **Ausgabe:** Jeder Wert aus Datenbank oder Anfrage wird beim Einsetzen maskiert – in
  HTML-Text und -Attributen mit `html.escape(str(wert), quote=True)`, in einem
  `<script>`-Block nur über `pages.script_json`, in einer Adresse innerhalb von Inline-JS
  mit `urllib.parse.quote(…, safe='')`. Im Browser-JS nie Daten per `innerHTML` oder
  Template-String in HTML setzen, sondern `textContent`, `createElement`, `.value = …`.
  Richtext (Einleitungs-/Schlusstext der Angebote) nur über die Positivliste
  `richtext.sanitize_richtext` (b, strong, i, em, u, p, br, ul, ol, li, div, span, ohne
  Attribute, höchstens 50 Ebenen tief): beim Speichern UND bei jeder Ausgabe – Formular wie
  PDF-Satz –, auch für Altbestand. Jede Antwort trägt
  `X-Content-Type-Options: nosniff` und `X-Frame-Options: DENY` (`end_headers`); die App
  rahmt sich nicht selbst ein (PDFs öffnen als eigene Seite in einem neuen Tab). Jede Antwort trägt
  genau einen `Cache-Control` (seit 2.5.15) – den des Handlers (`serve_static_file`), sonst `no-store` für
  `/backup/download`, `/receipts/file` und die Suchen und `no-cache` für alles andere; entschieden wird an der
  Route, nie an der Query, eine Endungs-Regel gibt es nicht (`tests/test_cache_headers.py`)
- **Logos** (seit 2.3.3): Als Logo gelten nur Bilddateien der Positivliste `export/pdf_core.LOGO_CONTENT_TYPES` (PNG, JPG, GIF,
  WEBP – kein SVG). Sie gilt an allen vier Stellen: Upload (`handle_logo_upload`: Endung UND Kennbytes müssen passen, Anfrage
  höchstens 5 MB, geprüft an `Content-Length` vor dem Lesen), Auslieferung (`/data/logos/`, `/seed_data/private/`: alles andere 404),
  Einbettung ins PDF (`resolve_logo_path`) und Hinweis im Kontaktformular (`logo_on_server`). `resolve_logo_path` nimmt vom
  gespeicherten Wert nur den Dateinamen und sucht ihn im `logos`-Verzeichnis des Benutzers (mit `realpath` geprüft; daneben nur
  `seed_data/private`): Ein frei eingetragener Serverpfad ergibt kein Logo – nicht aufweichen
- **Belegdateien (seit 2.5.11, `server/receipt_files.py`):** Jede Datei, die die Seite *Belege* hochlädt, wird sofort ein Beleg (Nummer aus
  dem Kreis „Belegnummern Firma“, Datum, Dateiname) und liegt unter `<Datenordner>/Belege/<Jahr des Belegdatums>/<Nummer>_<Name>`
  (`Database.insert_receipt_with_file`; `Documents.Path` relativ zum Datenordner, mit `/`). Den Pfad setzt der Server, nie der Client.
  Ein Kontoauszug ist dort ein Beleg wie jede andere Datei (`receipt_date` liest ihn nur für das Datum und den Hinweis auf den Import der
  Seite *Buchungen*): kein Import, nichts in `pending_imports/`; `POST /upload_receipts` bleibt der Import dieser Seite.
  **Upload** `POST /receipts/upload` (`_UPLOAD_PATHS`, JSON, GET → 405): vor dem ersten gelesenen Byte CSRF-Token (Header), Demo-Modus
  (403 mit `RECEIPTS_DEMO_REFUSED`, Verbindung zu; die Seite zeigt den Satz statt der Ablage) und Größe (`_read_body(MAX_UPLOAD_BODY)`),
  höchstens 100 Dateien je Upload. **Je Datei gehören Nummer, Datei und Eintrag zusammen:** Der Inhalt wird VOR der Transaktion als Teildatei
  (`<32 Hex-Zeichen>.part`, `O_CREAT|O_EXCL|O_NOFOLLOW`) in den Jahresordner geschrieben – das Schreiben von bis zu 200 MB hält keine
  Schreibsperre; der Jahresordner darf kein Link sein (`year_folder`, sonst scheitert die Datei und nichts ist angelegt; `Belege/` selbst darf einer
  sein). In EINER Transaktion (`BEGIN IMMEDIATE`) folgen Nummer, Name (`reserve_name` legt den Platz als leere Datei mit
  `O_CREAT|O_EXCL|O_NOFOLLOW` an: nie wird eine vorhandene überschrieben, ein belegter Name bekommt `_2`, `_3` …), `os.replace` der Teildatei auf
  diesen Platz und der Eintrag. Scheitert ein Schritt, auch das COMMIT, ist der Zähler nicht verbraucht (und kein Kreis angelegt), es gibt keinen
  Eintrag, und weder Datei noch Teildatei bleibt liegen (erst werden die Dateien entfernt, dann wird zurückgerollt; ein scheiterndes Zurückrollen
  verdeckt die Ausnahme nicht); die Antwort nennt je Datei nur feste Sätze (`RECEIPT_UPLOAD_FAILED` …), nie Ausnahmetext oder Pfad. Der Name wird
  bereinigt (`safe_receipt_name`: nur der Name, `[A-Za-z0-9ÄÖÜäöüß ._()-]`, höchstens 120 Zeichen, kein Windows-Gerätename); der abgelegte Name
  (`stored_file_name`: `<Nummer>_<Name>`) hat höchstens 200 Bytes in UTF-8 (die Nummer höchstens 60; gekürzt wird der Stamm, die Endung bleibt)
  und heißt nie `Konten` oder `general` – so heißen die Ordner des Kontoauszug-Imports im Jahresordner (ein Unterstrich davor). Der angezeigte Name
  (`display_name`) trägt weder Steuerzeichen noch unsichtbare Formatzeichen (Unicode-Kategorien `Cc` und `Cf`, auch U+202E).
  **Die Nummer** (`take_receipt_number`): der Kreis des Jahres des Belegdatums, sonst der jüngste – wie `peek_next_number`; gibt es noch gar keinen
  Kreis „Belegnummern Firma“, entsteht einer für das **laufende** Jahr (Buchstabe `B`, Standardvorlage, in der Transaktion des Belegs) – sonst nie von
  selbst, auch nicht zum Jahreswechsel, und eine eigene Nummer ohne Kreis bildet keinen; liefert die Vorlage eines Kreises keinen Zähler, hat der
  Beleg keine Nummer, die Antwort einen Hinweis. Keine Prüfung auf belegte Nummern (nicht eindeutig, derselbe Kreis nummeriert die Beleg-Nr. der
  Buchungen). Eine eigene Nummer hebt den Zähler eines Kreises nur, wenn sie zu dessen Vorlage passt und ihre laufende Nummer in 1 bis
  `MAX_RECEIPT_RUNNING` (999 999 999) liegt; eine größere gilt als Text, ohne Fehler. `consume_number` (die Buchungsmaske) hebt den Zähler auf die
  gespeicherte Nummer, wenn sie über ihm liegt – nie nach unten: Hat das Löschen eines Belegs den Zähler zurückgenommen, wird eine Nummer, die
  eine Buchung trägt, nicht noch einmal vergeben. Das Formular „Beleg hinzufügen“ (ohne Datei) verbraucht die Nummer ebenso (`number_suggestion`:
  eine unveränderte Nummer wird frisch vergeben) und nimmt nie einen Pfad. Beim Löschen des Belegs mit der zuletzt vergebenen Nummer geht der
  Zähler um eins zurück, wenn die Nummer genau die ist, die der Kreis für ihre laufende Nummer vergibt („26B0001“ ist nicht „26B001“), und kein
  anderer Beleg und keine Buchung sie trägt (ohne Rücksicht auf Groß-/Kleinschreibung; `release_receipt_number`, in der Transaktion des Löschens);
  eine Nummer mitten im Kreis bleibt eine Lücke und wird nie neu vergeben, umnummeriert wird nie. **Ansehen und Herunterladen**
  (`GET /receipts/file?id=<n>[&inline=1]`, rein lesend, ID über `_id_param`, Herkunft wie
  bei `/backup/download`): Ein gespeicherter Pfad wird nur über `receipt_files.stored_receipt_path` geöffnet oder gelöscht und gilt nur
  innerhalb von `Belege/` und `Documents/` (Bestand des früheren Uploads) des angemeldeten Benutzers – mit `realpath` aufgelöst, mit
  `commonpath` geprüft, höchstens `STORED_PATH_LIMIT` = 1024 Zeichen lang; alles andere ist 404 mit festem Satz, der Pfad steht nur im Server-Log. Geöffnet wird mit `O_NOFOLLOW`/`O_NONBLOCK`
  und `fstat` (nur reguläre Dateien), gesendet in Stücken (`backup.copy_to`, Zeitgrenze je Stück) mit `no-store`. Angezeigt wird nur mit
  `inline=1` UND wenn Endung und Kennbytes zur Positivliste passen (`inline_type`: PDF und die Rasterbilder der Logos); alles andere – auch
  SVG, HTML, XML – ist `attachment` mit `application/octet-stream`. Die Route steht nicht in der Positivliste des Diagnose-MCP.
  **Löschen** (`POST /receipts/delete`): Die Datei geht mit – nur, was der Upload selbst ablegt: der gespeicherte Wert `Belege/<JJJJ>/<Name>`
  (relativ, vier Ziffern, weder Jahresordner noch Datei ein Link; `receipt_files._uploaded_shape` – dieselbe Stelle entscheidet, ob die Rückfrage
  „und seine Datei“ lautet). Nicht der Bestand in `Documents/`, nicht, was der Kontoauszug-Import unter `Belege/<Jahr>/Konten/` oder
  `Belege/<Jahr>/general/` ablegt, kein absoluter Pfad, kein `..`; dazu nur eine reguläre Datei, nur wenn kein anderer Beleg auf dieselbe
  aufgelöste Datei zeigt, nie nach einer Ablehnung (`remove_receipt_file`). Das Formular ändert den gespeicherten Pfad nie.
  `tests/test_receipt_upload.py`, `tests/test_receipt_file.py`, `tests/test_delete_refusals.py`, `tests/test_suggestion.py`
- **Belege verknüpfen (seit 2.5.12, `server/receipt_links.py`, `Database.link_receipt_to_booking`):** An der Buchung (Maske, nach `</form>`, eigene
  `fetch`-Aufrufe, kein Formularfeld) wird ein Beleg gesucht und zugeordnet, am Beleg (`/receipts/edit?id=<n>`, ID über `_id_param`) eine Buchung.
  **Suchen:** `GET /receipts/search` und `GET /transactions/search` (`_SEARCH_PATHS`; `q` höchstens 100 Zeichen, sonst 400; `limit` 1–50, sonst 20, auch in
  der Datenbankschicht auf 1–50 begrenzt; Herkunft geprüft; rein lesend; ohne Rücksicht auf die Schreibweise über `ub_fold` + `INSTR`, kein `LIKE`; ein
  Datum oder dessen Anfang und ein Betrag finden ebenfalls; die Antwort nennt nie einen Pfad, nur `none`/`outside`/`ok`; `Cache-Control: no-store`, auch bei
  einem Suchbegriff wie `.png`; ihre 503 während einer Wiederherstellung kommt als JSON; nicht in der Positivliste des Diagnose-MCP; seit 2.6.0 steht eine dritte Route
  unter denselben Regeln in `_SEARCH_PATHS`: `GET /receipts/suggestions`, siehe *Belegerkennung*). **Schreiben:**
  `POST /bookings/link-document` und `/bookings/unlink-document` (`_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`, JSON `{bookingId, documentId[, relationType]}`,
  IDs streng über `_json_positive_id`; `relationType` aus `RELATION_TYPES`, sonst `receipt`; unbekannte Buchung oder Beleg 404 mit Klartext). Verknüpfen ist
  idempotent und läuft in EINER Transaktion: Prüfen, Zeile, Beleg-Nr. **Die übernommene Nummer wirkt wie eine von Hand eingetragene, auch auf den
  automatischen Abgleich** (dieselbe Beleg-Nr. am selben Tag fasst eigenständige Sätze zu EINEM Beleg zusammen, Stufe 4 vergleicht die Nummer einer
  Bankbewegung als Teilzeichenkette) – deshalb übernimmt die Buchung die Nummer des Belegs (`update_booking_fields(tx=…)`, Replay-Log erst nach dem Commit)
  nur, wenn **alle** Bedingungen zutreffen, in dieser Reihenfolge: (1) sie trägt keine Beleg-Nr. (leer oder nur Leerraum); (2) der Beleg trägt eine Nummer;
  (3) die Verknüpfung ist von der Art `receipt` – maßgeblich ist die Art, die nach dem Aufruf in `BookingDocuments` steht (eine bestehende Verknüpfung
  behält ihre, auch bei erneutem Zuordnen; `invoice`, `contract` und `other` übernehmen nie); (4) kein Zahlungssatz (`invoice_payment`); (5) keine Rechnung
  trägt die Nummer (`Invoices.InvoiceNumber`); (6) keine **andere** Buchung trägt diese oder eine ähnliche Nummer – ähnlich: die eine ist in der anderen
  enthalten, in beiden Richtungen, ohne Leerraum am Rand und ohne Rücksicht auf die Schreibweise (`26B001` gegen `26B0010` und `26B001_A`;
  `_NUMBER_IN_USE_SQL`, ein Wert aus einem Altbestand, der kein Text ist – Zahl, Blob –, gilt als sein Text). Die eigene Familie zählt dabei nicht (sie gehört
  zum selben Vorgang, der Abgleich fasst sie nicht an): die Sätze der Buchung, ihre Bankbewegung und deren übrige Sätze; eine fremde Bankbewegung und deren
  Sätze schon. **Diese Bedingungen nicht lockern** – lieber keine Nummer als eine, die der Abgleich als Beleg liest. Die Prüfung (6) liest die Tabelle einmal
  (kein Index auf `DocumentNumber`; gemessen rund 50 ms bei 200 000 Buchungen, unter der Schreibsperre). Nur die verknüpfte Zeile (Kinder einer Bankbewegung
  erben nichts, wie beim Speichern der Maske). Unterbleibt die Übernahme allein an (6), sagt es die Antwort (`numberInUse`) und die Seite („Die Beleg-Nr. wurde
  nicht übernommen: …“, als Hinweis, nicht als Erfolg); `hasEntries` ist wahr bei einer Bankbewegung mit Buchungssätzen, die Meldung sagt dann, dass diese ihre
  eigenen Nummern behalten. Lösen entfernt nur die Verknüpfung, die Nummer bleibt (sie bleibt auch beim Löschen des Belegs in Gebrauch). **Die Listen der Antwort**
  (`documents`, `bookings`) liest `handlers._link_lists` NACH dem Commit: Scheitert das, bleibt die Antwort 200 mit leeren Listen und `stale: true` – nie 500, die
  Verknüpfung ist gespeichert –, die Seite zeichnet dann nichts neu und bittet um Neuladen. **Eine Antwort gehört zur geöffneten Buchung:** `#txReceipts` trägt
  `data-booking`; `txAttach`/`txDetach` halten die ID beim Aufruf fest und prüfen im Antwortzweig als Erstes `txStill(<ID>)` (die Maske wird beim Öffnen einer anderen
  oder neuen Buchung ausgetauscht) – sonst fassen sie weder Liste noch Feld Beleg-Nr. noch Meldung an; die Treffer von `txReceiptSearch` werden ebenso nur dann
  gezeichnet. Die Seiten bekommen die Daten als JSON im Skriptblock (`script_json`);
  das Skript (`LINK_WIDGET_JS`, `RECEIPT_LINK_JS`, `TX_LINK_JS`) setzt sie nur über `textContent`/`createElement`/`.value`, nur `var` und `function`.
  **Bekannte Grenzen der übernommenen Nummer:** Bekommt eine aus dem WISO-Textexport importierte Buchung ohne Beleg-Nr. so eine Nummer, erkennt ein erneuter
  Import derselben Textdatei sie nicht wieder und legt sie ohne Meldung ein zweites Mal an; die Übernahme CSV → WISO-Datenbank nimmt sie nicht (neu angelegt,
  als mögliches Duplikat gemeldet); ein Satz mit bekannter Quellgruppe wird beim erneuten Import der Datenbank weiter wiedererkannt; legt WISO ihn unter neuer
  Quellgruppe neu an, findet die Neuzuordnung ihn nicht (neu angelegt, die alte Zeile gemeldet). Eine vor dem Zuordnen geöffnete Maske (zweites Fenster), die
  danach gespeichert wird, schreibt ihr leeres Feld Beleg-Nr. zurück; eine schon exportierte Buchung trägt nach dem Zuordnen ein Belegfeld 1.
  `/documents/link` und `/documents/unlink` gibt es nicht mehr. `tests/test_receipt_links.py`
- **E-Mail-Versand** (`email_sender.py`, seit 2.3.3): immer verschlüsselt und mit Zertifikatsprüfung (`ssl.create_default_context()`;
  Port 465 `SMTP_SSL`, sonst STARTTLS – ohne STARTTLS-Angebot wird nicht angemeldet). Es gibt keinen Schalter zum Abschalten; ein
  eigenes CA-Zertifikat kommt über `SSL_CERT_FILE` (`DEPLOYMENT.md`). Meldungen an den Nutzer nennen nie Exception-Text oder
  Serverpfade (der volle Fehler steht im Server-Log); Namen gehen über `formataddr` in den Kopf, CR/LF wird abgelehnt
- **Anfragegrößen (seit 2.3.6, `server/app.py`):** Kein Body wird gelesen, bevor seine erklärte Länge geprüft ist
  (`_declared_length`: genau EIN `Content-Length` aus ASCII-Ziffern, kein `Transfer-Encoding`; sonst 400). Grenzen: 64 KB ohne
  angemeldeten Benutzer (`MAX_PREAUTH_BODY`: `/login`, `/logout`, Ersteinrichtung), 10 MB Formulare und JSON (`MAX_FORM_BODY`),
  200 MB Uploads (`MAX_UPLOAD_BODY`: `/upload_receipts`, `/receipts/upload`, `/wiso/import`), Logos 5 MB; darüber 413,
  ungelesen, Verbindung zu (`_refuse_body`). Neue Routen lesen ihren Body über `_read_body(limit)` bzw. prüfen vorher mit `_check_body_size` – nie
  `rfile.read(int(Content-Length))`. `_drain_body` liest höchstens 1 MB weg, sonst schließt es die Verbindung. Einzige größere
  Grenze: die WISO-Datenbank mit 2 GB je Datei (`wiso_upload.MAX_WISO_DB_BODY`, `/wiso/fdb-upload`) – sie wird nie im Ganzen
  gelesen, sondern in Stücken auf die Platte geschrieben, und nur, wenn danach noch 64 MB frei bleiben (sonst 507)
- **Zeitgrenze je Verbindung (seit 2.5.8, `server/app.py`):** Auf einer Verbindung, auf der `CONNECTION_TIMEOUT` = 120 s lang nichts mehr
  gelesen oder gesendet wird, gibt der Server auf (`SimpleWebServer.timeout`: je Lese- bzw. Sendeschritt, nicht für die ganze Anfrage).
  Ein Client, der seinen Body nicht zu Ende schickt, die Antwort nicht abholt oder eine Keep-Alive-Verbindung offen lässt, hält damit
  weder einen Thread noch die Lesesperre des Datenverzeichnisses (`_run_locked`) länger. Antworten gehen in Stücken von `SEND_CHUNK` =
  256 KiB hinaus (`_SteppedWriter`; sonst gälte die Grenze für die ganze Antwort); nach einem Fehlschlag wirft jeder weitere Schreibzugriff
  sofort, eine Fehlerantwort wartet nicht noch einmal die Frist ab. Ein Body, der nicht zu Ende kommt: 408 und Verbindung zu
  (`_read_body`); wo ein Handler den Body selbst liest, schließt `_TimedReader` die Verbindung in jedem Fall. Die Bearbeitung selbst hat
  keine Zeitgrenze (die SQL-Konsole hat ihre eigenen 60 s), und ein Upload, der stetig sendet, läuft beliebig lange. Nicht abschalten:
  Die Lesesperre des Datenverzeichnisses hinge sonst an einer hängengebliebenen Verbindung (`tests/test_connection_timeout.py`).
  **Jede Anfrage wird abgeschlossen** (`_settle_body`, nach der Antwort in `handle_one_request`): Was vom erklärten Body ungelesen
  blieb – eine GET-Anfrage mit `Content-Length`, eine frühe Ablehnung –, darf nie als nächste Anfrage gelesen werden. `_TimedReader`
  zählt die gelesenen Bytes (`count`, nach den Kopfzeilen 0); ein Rest bis `MAX_DRAIN_BODY` wird mit `DRAIN_TIMEOUT` = 2 s weggelesen
  (dann bleibt die Verbindung benutzbar), bei größerem Rest, `Transfer-Encoding`, ungültiger Länge oder ausbleibendem Rest geht sie zu.
  Ein neuer Zweig braucht dafür nichts zu tun – die Prüfung nicht umgehen (`tests/test_request_body_sync.py`). **Log:** Eine
  Verbindung im Leerlauf wird still geschlossen; ließ sich eine Antwort nicht zustellen (`wfile.failed`), reichen die breiten
  `except`-Zweige den Fehler weiter (`if self.wfile.failed: raise`), statt eine 500 zu versuchen – es bleibt eine Zeile im Log.
  **TLS (seit 2.5.9):** Bei eigenem TLS (`build_server`) läuft der Handshake je Verbindung in ihrem Thread
  (`SimpleWebServer.setup()`, Zeitgrenze `TLS_HANDSHAKE_TIMEOUT` = 15 s für den ganzen Handshake), nie in `accept()`
  (`do_handshake_on_connect=False`): Die Annahme neuer Verbindungen wartet nie auf einen Handshake – nicht zurückdrehen. Ein
  gescheiterter Handshake endet still (`handle()`: kein Log-Eintrag, keine Antwort; `tests/test_tls_handshake.py`). Die Warteschlange
  angenommener Verbindungen ist `ACCEPT_QUEUE` = 128 lang (mit und ohne TLS; der Standard der Bibliothek, 5, ließ bei vielen
  gleichzeitigen Verbindungen einzelne im Betriebssystem liegen)
- **Anmeldung (seit 2.3.6, `auth.attempt_login`):** Die Anmeldung im Browser geht nur darüber (`handlers.handle_login` →
  `LoginOutcome`), nie direkt über `authenticate`. Nach fünf Fehlversuchen in Folge ist der Name (ohne Groß-/Kleinschreibung)
  30 s gesperrt, je weiterem Fehlversuch doppelt, höchstens 900 s (429 mit `Retry-After`); während der Sperre wird nichts geprüft,
  gezählt oder verlängert, auch nicht mit dem richtigen Passwort. Gezählt wird VOR dem Hash (parallele Versuche zählen mit).
  Unbekannte Namen verhalten sich wie vorhandene (eigene, auf 10000 Einträge begrenzte Tabelle; Einträge vorhandener Benutzer
  werden nie verdrängt). Höchstens zwei scrypt-Läufe gleichzeitig (`_HASH_SLOTS`; nach 10 s Warten 503). Alles im Speicher: Ein
  Neustart hebt Sperren auf. Das Server-Log nennt bei einer Sperre die Adresse, nie den eingegebenen Namen (dort landet
  gelegentlich ein Passwort). Mindestlänge beim Setzen eines Passworts: 8. `set_password` beendet die Sitzungen des Benutzers
  (außer `keep_session`: wer sein eigenes ändert), das Ein- und Ausschalten der automatischen Anmeldung alle
  (`delete_all_sessions`). Tests: `conftest` leert die Drosselung vor jedem Test (`reset_login_throttle`)
- **`Secure`-Cookie:** bei eigenem TLS oder wenn ein Proxy `X-Forwarded-Proto: https` meldet (`forwarded_https`, erster Wert).
  Der Kopf steuert nur das Cookie-Flag, sonst nichts
- **IDs aus der Adresse** nur über `_int_param` (Filter: fehlt/ungültig = nicht angegeben), `_id_param` (Seite zu einem Objekt:
  fehlt/ungültig = −1, also wie eine unbekannte ID) und `self._query()`; nie `int(qs[…][0])` oder `split('?')[1]`. Was nicht in
  64 Bit passt, gilt als ungültig. Aktionen ohne gültige ID: 400 (`tests/test_bad_addresses.py` prüft `app.py` statisch)
- **Formularwerte (seit 2.4.2, `server/handlers.py`):** IDs aus Formularen nur über `_form_id` (positive ASCII-Ganzzahl in 64 Bit; optionale
  Felder über `_optional_form_ids`), Ganzzahlen nur über `_form_int(text, minimum, maximum, default)` (ASCII-Ziffern im Bereich: Jahr 1900–2999,
  Prozent 0–100, Nutzungsdauer 1–100, Zählerstand 0–999 999 999), Kommazahlen nur über `_finite_number` – nie `int(post_data…)`/`float(post_data…)`.
  Ungültig: die Meldung auf dem Fehlerweg der Seite (303 mit `err=`/`error=`/`msg=`, Klartext über `quote(…, safe='')`, angezeigt nur über
  `script_json` + `appMsg`); bei einer ungültigen ID und auf Seiten ohne Fehlerweg 400 mit Klartext. Nichts wird gespeichert. Ein Handler gibt
  `(Status, Text)` zurück, `server/app.py` antwortet über `_respond_form_result`: ab 400 steht der Text im Body (`text/plain`), nie im
  `Location`-Kopf (sonst bliebe die Seite leer). **Nie Exception-Text in einer Antwort:** ein fester Satz, der volle Fehler nur ins Server-Log
  (`traceback.print_exc()`); Klartext kommt nur aus eigenen Ablehnungsklassen (`_Refused`, `DeleteRefused`, `ContactRefused`,
  `BackupRefused` …). Eine doppelte Kunden-/Kontaktnummer oder ein doppeltes Kürzel wirft `ContactRefused` (Rollback, Meldung
  auf der Kontaktseite). `tests/test_form_error_paths.py` prüft es und hält statisch fest, dass `handlers.py` weder
  `int(post_data…)`/`float(post_data…)` noch einen 5xx mit Ausnahmetext noch eine Weiterleitung (3xx) mit dem Text einer
  Ausnahme aus einem breiten `except` enthält. **Die Meldungen auf *Sonstiges* (seit 2.5.5, `msg=`):** Sicherung,
  Wiederherstellung, DATEV-Export und WISO-Import (Textexport wie Datenbank) geben nur den Klartext einer `BackupRefused`
  weiter, sonst einen festen Satz (`BACKUP_FAILED`, `RESTORE_FAILED`, `DATEV_FAILED`, `WISO_IMPORT_FAILED`); der volle Fehler
  steht im Server-Log. Die Seite liest `msg` nicht im Browser, sondern bekommt es vom Server als JSON im Skriptblock `miscMsg`
  (`script_json`, höchstens `MSG_MAX_LENGTH` = 2000 Zeichen – die längste Meldung des DATEV-Exports hat gut 1500) und zeigt es
  nur über `textContent`/`appMsg` (`tests/test_misc_messages.py`). Der Beleg-Upload (`server/upload_handler.py`) nennt je Datei nur
  den Klartext eigener Klassen (`CamtError`, `DkbCsvError`, `document_parser.DocumentExists`), sonst `UPLOAD_FAILED`; die Belege-Seite
  zeigt das Ergebnis je Datei als Text und lädt nur ohne Warnung neu (`tests/test_upload_feedback.py`)
- **Fehlerantworten über `send_error`** (`server/app.py`): tragen immer Statuszeile und Schutz-Header, auch bei einer kaputten
  Anfragezeile – unabhängig vom Patch-Stand der Standardbibliothek (eine echte HTTP/0.9-Anfrage bekommt ihre normale Antwort
  weiter ohne Kopf)
- **Wiederherstellen** (`server/backup.py`): Ein 7-Zip-Archiv wird vor dem Entpacken
  aufgelistet (`-t7z`, keine Links, keine absoluten oder `..`-Pfade), dann nach
  `backup/restore_*` entpackt, geprüft (keine Symlinks, Datenbank lesbar und nicht neuer
  als das Programm) und erst danach ersetzt. Der vorherige Stand wird nach
  `backup/replaced_*` verschoben, nicht vorab gelöscht, und erst nach Erfolg entfernt
  (`replaced_*` nie automatisch aufräumen). Über Dateisystemgrenzen wird kopiert statt
  umbenannt; beim Überschreiben kommt `buch.db` zuletzt; „vorher löschen“ mit einer
  Nur-Datenbank-Sicherung wird abgelehnt. Sicherung und Wiederherstellung sperren sich
  je Datenverzeichnis (`_exclusive`). Diese Reihenfolgen nie umdrehen. Nie gesichert, gelöscht oder überschrieben werden
  `backup/`, `users/`, `tests/` und `upload_tmp/` (`_EXCLUDED_DIRS`, eine Liste für alle drei Wege). **Meldungen:** Eine
  Ablehnung des Moduls ist `BackupRefused` (eine `RuntimeError`; ein unbekanntes Archiv oder ein Link als Archiv
  `BackupNotFound`, zugleich `FileNotFoundError`) mit Klartext – nie ein Serverpfad, nie die Ausgabe von 7-Zip, nie der Text
  einer Systemausnahme: Die stehen im Server-Log (`from e`, `print`). Scheitert `create_backup`, wird das Teilarchiv entfernt
  (der Name war frei, es ist nie eine fremde Datei); `list_backups` nennt nur reguläre Dateien, keine Ordner oder Links;
  `_next_archive_path` vergleicht ohne Groß-/Kleinschreibung.
  Scheitert beim Überschreiben erst der Schritt der Datenbank, sagt die Meldung den Stand (`OVERLAY_DB_FAILED`; bleibt eine
  Begleitdatei der alten Datenbank liegen: `OVERLAY_SIDECARS_LEFT` – die übrigen werden trotzdem entfernt). **Wiederherstellen läuft
  allein (seit 2.5.6, `server/dirlock.py`):** Jede Anfrage eines angemeldeten Benutzers läuft unter der Lesesperre seines
  Datenverzeichnisses (`SimpleWebServer._run_locked`; `do_GET`/`do_POST` bestehen nur aus `_gate()` und `_run_locked` – keinen Zweig
  daran vorbei bauen; ausgenommen `_LOCK_FREE_ROUTES`: `/backup/download`, `/buch.css`, `/favicon.ico`, nur GET), `handle_backup_restore`
  nimmt die ausschließliche: Neue Anfragen bekommen sofort 503 mit `Retry-After` (als JSON auf den Routen, die ihre Seiten als JSON
  lesen: `_JSON_ANSWER_ROUTES`), auf laufende wartet es höchstens `RESTORE_WAIT_SECONDS` = 30 s, sonst wird abgelehnt und nichts
  geändert. Ob es das Archiv gibt, prüft der Handler VOR der ausschließlichen Sperre (`backup.has_archive`): Ein Versuch, der nie
  beginnen kann, hält keine Anfrage auf. Im Demo-Modus (alle Besucher teilen ein Verzeichnis) wartet das Wiederherstellen nur
  `DEMO_WAIT_SECONDS` = 2 s und ruht nach einer Ablehnung `DEMO_COOLDOWN_SECONDS` = 60 s (neue Versuche werden sofort abgelehnt, ohne
  Anfragen aufzuhalten) – nicht verlängern bzw. streichen. Reihenfolge der Sperren: `dirlock` außen, `_exclusive` innen – nie umdrehen. Die Sperre gilt in einem Prozess; nicht
  erfasst sind ein zweiter Serverprozess auf demselben Datenordner, Zugriffe von Hand und der Umzug beim Start
  (`tests/test_dirlock.py`)
- **Sicherungen herunterladen (seit 2.5.5, `GET /backup/download?file=<Name>`):** Die Seite *Sonstiges* listet die Sicherungen
  des angemeldeten Benutzers (Name, Art, Datum, Größe) mit einem Link. Angeboten wird allein, was `backup.downloadable_backups`
  nennt – eine Positivliste: die Archive und die Kopien vor einer Programm-Umstellung (`<Datum>_vor_<Version>[_n]_buch.db`, nur
  herunterladbar, nicht im Formular der Wiederherstellung), nur reguläre Dateien direkt in `backup/`, nie
  `restore_*`/`replaced_*`, `.part`/`.tmp`, Verstecktes, Ordner, Links oder Namen mit Pfadanteil; jede andere Anfrage ist
  dieselbe 404 ohne Pfad. Geöffnet wird mit `O_NOFOLLOW` und gegen `realpath` geprüft (`backup/` selbst darf ein Link sein),
  kurz unter `_exclusive` (das Öffnen wartet bis `DOWNLOAD_WAIT_SECONDS` = 5 s – zwei Downloads lehnen einander nicht ab –,
  läuft eine Sicherung oder Wiederherstellung länger: 409; Sicherung und Wiederherstellung selbst warten nie); geliefert wird
  **ohne die Sperre in Stücken** (`copy_to` liest je 1 MiB, gesendet wird in Schritten von `SEND_CHUNK`) aus dem offenen
  Deskriptor, nie ganz im Speicher – eine Sicherung kann Gigabyte groß sein. Je Stück gilt eine Zeitgrenze (`DOWNLOAD_SEND_TIMEOUT` = 120 s: Ein Client, der nicht mehr liest, hält
  Verbindung und Datei nicht unbegrenzt), und die Antwort trägt `Cache-Control: no-store`. Ein `Range`-Kopf wird überhört,
  `HEAD` gibt es nicht; ein Fehler nach den Köpfen schließt die Verbindung, statt eine zweite Antwort zu schreiben. Ein
  Dateiname, der sich nicht als UTF-8 schreiben lässt, wird weder gelistet noch geliefert. Die Route liest nur (kein CSRF-Token, wie die übrigen GET-Downloads), prüft aber die Herkunft
  (`request_is_same_origin`: eine fremde `Sec-Fetch-Site` ergibt 403) und steht **vor `_open_db()`**: Sicherungen lassen sich
  auch holen, wenn sich die Datenbank nicht öffnen lässt. Jeder angemeldete Benutzer, nur aus dem eigenen Ordner; im Demo-Modus
  wie Sichern und Wiederherstellen. Nicht aufweichen (`tests/test_backup_download.py`)

### Belegerkennung (seit 2.6.0)
- **Grundsatz:** Ein gelesener Wert ist eine Lesung der Datei, nicht der Inhalt des Belegs. Die Lesung steht in `DocumentReadings` (eine Zeile je
  Beleg, `db/document_readings.py`), getrennt von dem, was der Nutzer am Beleg pflegt (`Documents`: Nummer, Datum, Dateiname, Info). **Nichts wird
  ohne Klick verknüpft, gebucht oder am Beleg geändert – nicht einführen.** Umfang heute: Felder lesen und mit ihrer Herkunft zeigen, eine
  Bankbewegung vorschlagen. Kein OCR (ein Scan oder Bild ist `no_text`), kein Sprachmodell. Beträge stehen in Minor Units (`money.SCALE` = 4, also
  nicht Cent), der Steuersatz als Anteil (0,19)
- **Das Paket `receipt_reading/` ist rein:** Es bekommt Bytes, schreibt nichts, öffnet keine Dateien und kennt weder `server/` noch `db/`;
  `read_document` wirft nie (jede Ausnahme ist eine Lesung `failed` mit Befund `read_error`, ohne Ausnahmetext). **Erkannt wird nach Endung UND
  Kennbytes** (`detect.kind`: `.pdf` mit `%PDF-`, `.xml` mit `<` nach BOM und Leerraum, Bild = Positivliste der Logos), nie nach nur einem von beiden.
  **Reihenfolge:** zuerst die E-Rechnung (`einvoice.py`: hochgeladenes XML – UBL `Invoice`/`CreditNote`, CII –, bei einer PDF das eingebettete
  `factur-x.xml`/`zugferd-invoice.xml`/`xrechnung.xml`, `pdf_embedded.py`); liefert sie kein lesbares Feld (`einvoice_empty`), kommen die Textregeln
  (`pdf_text.py`, `text_rules.py`). Gelesen werden BT-1 Nummer, BT-2 Datum, BT-5 Währung, BT-27 Verkäufer (UBL ersatzweise BT-28), BT-31 USt-IdNr.,
  BT-109 Netto, BT-110 Steuer, BT-112 Brutto, der Satz aus BG-23 (nur, wenn die Aufschlüsselung genau einen nennt) und die Art (BT-3 bzw. Wurzel:
  `invoice`/`credit_note`; eine Gutschrift trägt positive Beträge); BT-115 steht nur in `raw`
- **Strenge Werte, Befunde, Version, Grenzen:** Nur ASCII-Ziffern, ein Betrag mit höchstens zwei Nachkommastellen und zwölf Stellen davor, ein
  Kalenderdatum der Jahre 1900–2200, die Währung als drei Großbuchstaben, ein Betrag, dessen `currencyID` von BT-5 abweicht, gilt nicht – **was nicht
  streng passt, bleibt leer und bekommt den Befund `bad_<Feld>`, nie geraten**. Abgeleitet wird genau ein Wert: die Steuer 0, wenn der Gesamtbetrag
  der Steuer fehlt und die Aufschlüsselung nur Nullen nennt (Beleg ohne Steuerausweis). **Status** (`STATUSES`): `ok`, `empty` (lesbares Dokument ohne
  lesbares Feld), `no_text`, `unsupported`, `too_large`, `failed`; **Quelle** (`SOURCES`): `einvoice_cii`, `einvoice_ubl`, `text`. Die Befund-Codes
  stehen als Tabelle im Kopf von `receipt_reading/__init__.py`. **`READER_VERSION` steigt um 1, sobald sich die Leseregeln ändern:** Ältere Lesungen
  gelten dann als ungelesen (`unread_receipt_ids`) und zeigen „(ältere Regeln)“; neu gelesen wird nie still. **Grenzen** (Konstanten, bei jedem Aufruf
  gelesen – die Tests verkleinern sie): `MAX_READ_BYTES` 30 MB (darüber `too_large`, ohne Prozess), `MAX_XML_BYTES` 20 MB (auch entpackt),
  `MAX_EXTRACT_CHARS` 200 000 Zeichen aus einer PDF – gelesen werden nur die ersten 5 und die letzten 3 Seiten (`pdf_text.HEAD_PAGES`/`TAIL_PAGES`;
  unter 20 sichtbaren Zeichen: `no_text`) –, gespeichert höchstens `MAX_TEXT_CHARS` 20 000 Zeichen Text; `Reading.to_row` kappt Absender (200
  Zeichen), USt-IdNr. (20), Nummer (100) und `Raw` samt Befunden (`MAX_RAW_BYTES` 64 KB: erst fallen Zitate und Aufschlüsselung weg, Befund
  `raw_truncated`)
- **Schutz (die Datei kommt aus einem Upload) – nicht aufweichen:** XML nur über `importers.safe_xml.parse_untrusted_xml` (Größe vorab, jede DTD und
  Entity-Deklaration abgelehnt, `UnsafeXml`; `importers/camt.py` nutzt dieselbe Stelle). Der Anhang einer PDF wird nur begrenzt entpackt
  (`pdf_embedded._bounded_inflate`: nur ein Flate-Filter ohne Predictor, höchstens `MAX_XML_BYTES` + 1 Bytes, nie `PDFStream.get_data()`;
  höchstens `MAX_ATTEMPTS` = 3 Anhänge mit dem Namen einer E-Rechnung werden entpackt – dieselbe Datei über `AF` und den Namensbaum, gleiches
  Stromobjekt, zählt einmal –, danach endet die Suche mit dem bisherigen Befund `attachment_unreadable`). **Gelesen
  wird im Kindprozess:** `receipt_reading.isolated.read_isolated` startet `python -m receipt_reading.worker` (derselbe Interpreter, Datei und Angaben
  der eigenen Firma über stdin) mit hartem Zeitlimit `READ_TIMEOUT` (15 s; Überschreitung: `too_large` mit Befund `time`), unter POSIX mit `RLIMIT_AS`
  und `RLIMIT_CPU`; die Ausgabe (höchstens 1 MB) wird streng über `Reading.from_dict` gegengeprüft, alles andere ist `failed` mit
  `reader_output`/`reader_crashed`. `read_isolated` wirft nie und nennt nie den Text des Kindprozesses (stderr steht höchstens gekürzt im Server-Log);
  der Server ruft `read_document` nie selbst auf
- **Textregeln (`text_rules.py`) – lieber leer als geraten:** Gearbeitet wird auf Zeilen (höchstens `MAX_LINES` zu je `MAX_LINE_CHARS` Zeichen,
  Leerraum zusammengefasst), jeder reguläre Ausdruck läuft in linearer Zeit. Ein Betrag ist nur `1.234,56`/`119,00` (Komma und genau zwei Stellen,
  Tausenderpunkte in Dreiergruppen) und kommt allein aus `money.parse_amount`; `19,00 %`, ein Betrag mit nicht eindeutigem **Vorzeichen** und der
  Rest einer Gruppierung (`1 234,56`) sind keiner. Nicht eindeutig ist das Vorzeichen, wenn davor ein Minus, Gedankenstrich oder eine öffnende Klammer
  steht (`- € 119,00`, `-EUR 119,00`, `(119,00)`) oder dahinter ein Minus oder ein Soll-/Haben-Zeichen (`119,00 -`, `119,00 €-`, `119,00 CR`/`DR`/`S`/`H`),
  dazwischen nur Leerraum und ein Währungszeichen (`_SIGN_BEFORE`/`_SIGN_AFTER`, EINE Stelle in `_scan_amounts`); `-119,00` unmittelbar davor ist ein
  negativer Betrag, ein Minus vor einem Buchstaben (`EUR-Konto`) oder mit Leerraum davor und einer Ziffer dahinter (`119,00 -5,00`) kein Vorzeichen
  dieses Betrags. **Steuer:** Zeilen mit Steuerwort (nicht die der USt-IdNr./Steuernummer, nicht die, in denen eine Brutto- oder Nettobezeichnung den
  Betrag beansprucht); der Betrag steht HINTER dem Steuerwort. Steht dort genau einer, ist er die Steuer – außer „auf“/„von“/„aus“ steht als Wort
  dazwischen (`19 % MwSt auf 100,00`: die Basis; die Zeile liefert keine Steuer). Stehen dort mehrere und nennt die Zeile genau einen Satz
  (`USt 19% 100,00 19,00 119,00`), ist es der Betrag, der als EINZIGER (gleiche Werte zählen einmal) die Satzprobe gegen einen ANDEREN Betrag der Zeile
  besteht (Basis × Satz auf den Cent – dieselbe Funktion wie die Satzprobe `_matches_net` – oder im Brutto enthalten); kein oder mehr als ein solcher
  Betrag, kein oder mehr als ein Satz oder mehr als `MAX_LINE_AMOUNTS` = 8 Beträge in der Zeile (eine Tabellenzeile; die Probe ist paarweise): keine
  Steuer, auch kein Befund. Bezeichnungen zählen als ganze Wörter und haben **Ränge** (Brutto: Rechnungsbetrag, Gesamtbetrag … vor
  Zahlbetrag vor „brutto“/„total“; Netto und Datum ebenso, eine Zeile mit Leistungs-, Liefer-, Fälligkeits- oder Bestellbezeichnung liefert kein
  Belegdatum): Nur der beste Rang, der überhaupt einen Treffer hat, zählt, der Betrag ist der erste HINTER der Bezeichnung derselben Zeile (steht die
  Gegenbezeichnung dazwischen, zählt das Paar nicht). **Mehrere verschiedene Werte für ein Feld: leer mit Befund `conflict_<Feld>`** – bei Brutto,
  Netto und Steuer nur dann nicht, wenn GENAU EINE Kombination die Summenprobe besteht (`by_checksum`). Der Absender ist nur ein Abschnitt der ersten
  Zeilen der ersten Seite, der wie ein Firmenname aussieht (Rechtsform als ganzes Wort); bei mehreren entscheidet der Fuß der letzten Seite, sonst
  `conflict_sender_name`. Die Proben stehen in `raw['checks']` (`sum`: Netto + Steuer = Brutto; `rate`: Netto × Satz = Steuer auf den Cent, nur bei
  genau einem Satz; je `ok`/`mismatch`/`incomplete`), **jedes gelesene Feld nennt Zeile und Seite**
  (`raw['fields'][<Feld>] = {value, source: 'rule', quote, page}`). **Ein Kontoauszug ist kein Beleg:** Erkennt ihn ein Importmodul
  (`importers.find_importer`) UND steht eine Saldozeile (`find_balances`) da, ist die Lesung `unsupported` mit Befund `statement` (der Text bleibt
  gespeichert). **Die eigene Firma** (`Database.own_identity`: Namen und USt-IdNrn. aller Kontakte mit `ContactType = 'own'`) ist nie der Absender und
  nie die USt-IdNr. des Belegs. **Die Währung gilt am Betrag** (`_currency_at`): Maßgeblich ist, was unmittelbar am gelesenen Betrag steht (davor oder
  dahinter, nur Leerraum dazwischen) – `€`/`EUR` ist Euro, `$`, `£`, `USD`, `CHF`, `GBP` machen den Betrag fremd –, am übernommenen Brutto, ohne Brutto
  an Netto und Steuer: Ein fremdes Zeichen dort lässt Beträge, Satz und Aufschlüsselung leer (`foreign_currency`), ein Euro-Zeichen setzt `EUR`
  (Zitat: die Zeile dieses Betrags). Steht am Betrag nichts, gilt der ganze Text wie bisher (Euro, wenn er Euro nennt) – aber ein fremdes Kürzel an
  einer Zahl und Euro nirgends an einer Zahl (nur „EUR-Konto“, „Preise in EUR“) macht die Beträge ebenfalls fremd. Lockern nur mit Tests für das,
  was NICHT gelesen wird
- **Gelesen wird nur auf Anfrage der Seite, nie im Upload selbst** (`server/receipt_reader.py`): `POST /receipts/read` (JSON `{documentId}`, ID streng
  über `_json_positive_id`; `_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`: GET 405, CSRF-Token und Herkunft wie jede JSON-Route) liest die Datei EINES
  Belegs; im Demo-Modus 403 (kein Knopf, nichts geschieht). **Es läuft höchstens ein Lesen je Prozess** (`_read_lock`), ein zweites bekommt sofort 409
  (`busy: true`). Ablauf von `read_receipt`: Beleg lesen (unbekannt: 404) → die Datei NUR über `receipt_files.open_receipt` (`Belege/`/`Documents/`,
  `O_NOFOLLOW`, reguläre Datei; ohne eine solche: Lesung `failed` mit Befund `no_file`) → Größe prüfen, Bytes in Stücken lesen, Datei schließen →
  `read_isolated` – **alles außerhalb einer Transaktion der Datenbank** – → `Database.save_document_reading(document_id, expected_path, row)`: Prüfung
  und Schreiben in EINER `BEGIN IMMEDIATE`-Transaktion, geschrieben wird nur, wenn der Beleg noch da ist und denselben Pfad trägt wie beim Lesen
  (sonst 409, nichts gespeichert); die vorhandene Lesung wird ersetzt. **Die Antwort nennt nie einen Pfad, den Namen der Datei auf der Platte, ihren
  ganzen Text (`Text`) oder einen Ausnahmetext:** feste Sätze, der volle Fehler nur im Server-Log (500); bei Erfolg 200 mit `reading`
  (`reading_payload`; dazu gehören die Zitate der gelesenen Zeilen, je höchstens 200 Zeichen) – auch für `failed`/`no_text`: Der Status sagt, was geschah. Am Beleg selbst ändert sich nichts. Die Seite *Belege* liest nach einem Upload die angelegten
  Belege nacheinander (`readReceipts`: je Beleg EINE Anfrage, bei `busy` nach 2 s noch einmal), dazu ein Kasten über der Liste mit einem Knopf für
  ungelesene (`count_unread_receipts`, höchstens `READ_BATCH` = 100 je Klick) und auf der Seite eines Belegs „Lesen“/„Neu lesen“. **Die Lesung ist
  abgeleitet:** ihre Schreibwege gehen nicht ins SQL-Protokoll (`_log_sql`), und sie geht beim Löschen des Belegs in dessen Transaktion mit
  (`delete_receipt`, ebenso die Vorlage „Zeitbereich löschen“ der SQL-Konsole) – sie zählt nie als Verweis im Sinn von `DeleteRefused`
- **Anzeige:** Spalte „Gelesen“ der Belegliste aus EINER Abfrage (`document_reading_summaries`, liest nie `Text` oder `Raw`; `summary_text`: Betrag ·
  Absender, sonst das Wort zum Status, mit „(ältere Regeln)“, `–` ohne Datei, `ungelesen` ohne Lesung; Kopf- und Datenzelle tragen die Klasse
  `readCell`, eine Mindestbreite in `buch.css`, damit der Text in schmalen Fenstern nicht auf viele Zeilen umbricht). Der Kasten „Gelesen“ auf der Seite eines
  Belegs (`pages_receipts._reading_box`, serverseitig, jeder Wert maskiert) nennt Status, Herkunft und Zeitpunkt (gespeichert UTC, gezeigt Ortszeit),
  die Felder mit ihrer Herkunft (bei den Textregeln das Zitat der Zeile, bei der E-Rechnung der BT-Bezeichner), die Proben (eine, die nicht stimmt, in
  Warnfarbe) und die Befunde – **nur als feste Sätze aus `receipt_reader.NOTE_TEXTS`:** ein unbekannter Schlüssel erscheint nie, und ein neuer
  Befund-Code im Kopf von `receipt_reading` braucht dort einen Satz (`test_every_note_code_of_the_reading_package_has_a_sentence`). `Raw` wird streng
  gelesen (kaputtes JSON ist leer), dazu steht immer der feste Hinweis `READING_HINT` („bitte prüfen“). **„Gelesenes Datum übernehmen“ setzt nur den
  Wert des Datumsfelds** (`receiptTakeDate`): gespeichert wird erst mit „Beleg aktualisieren“
- **Vorschläge Beleg → Buchung** (`db/receipt_matching.py` rein, `Database.suggest_bookings_for_receipt`, rein lesend): Es wird nur vorgeschlagen, nie
  verknüpft. Voraussetzung ist eine Lesung mit Status `ok`, Währung leer oder `EUR` und einem Brutto ungleich 0 (`usable_gross`) sowie ein Datum – das
  gelesene (`DocDate`), sonst das Belegdatum. **Nur, solange der Beleg an keiner Buchung hängt:** Hat er eine Zeile in `BookingDocuments` (gleich welcher
  Art der Verknüpfung, auch an einem Kind), liefert `suggest_bookings_for_receipt` `[]` – geprüft auf der Verbindung, die den Beleg liest, vor den
  Kandidaten (`Database.receipt_is_linked` stellt dieselbe Frage allein); nach dem Lösen der letzten Buchung kommen die Vorschläge wieder. **Kandidaten** (`receipt_match_candidates`: EINE Abfrage, alle Werte als Parameter, höchstens
  `MAX_CANDIDATES` = 200, älteste zuerst): Bankbewegungen und eigenständige Sätze, deren Betrag ohne Vorzeichen genau das Brutto ist (−119,00 passt zu
  119,00), mit Buchungsdatum von 7 Tagen vor bis 90 Tage nach dem Datum (`candidate_window`). **Ausgeschlossen:** das Kind einer Bankbewegung (sein
  Betrag ist ein Teil), der Zahlungssatz einer Rechnungszuordnung (`PAYMENT_MARK`), ein Spiegel (`AutoMirror` oder eine der `MIRROR_FORMS`; eine Zeile
  ohne Form gilt nicht als Spiegel), die Forderungsbuchung einer eigenen Rechnung (`_receivable_sql`, dieselbe Bedingung wie im Editor) und eine
  Buchung, an der – oder an einem ihrer Kinder – dieser Beleg schon hängt, gleich welcher Art. **Punkte** (`rank_candidates`, je Signal ein fester
  Satz als Begründung, der Betrag zuerst): Rechnungsnummer des Belegs im Text, in der Beleg-Nr. oder im Empfänger der Buchung (`POINTS_NUMBER` = 100;
  normalisiert mindestens 4 Zeichen mit einer Ziffer, rein numerisch erst ab 5), gemeinsame Namenswörter (30, ab zwei 40; mindestens 4 Zeichen, ohne
  `STOP_WORDS`), ein Buchungsdatum nahe dem Belegdatum (bis 20, je 3 Tage Abstand einer weniger). **Zahlungsrichtung** (`direction_fits`, nach `Kind` der
  Lesung): Eine Rechnung (oder ein Beleg ohne Art) erwartet einen Ausgang (Betrag der Buchung < 0), eine Gutschrift einen Eingang (> 0); passt die Richtung
  nicht, trägt der Vorschlag die Begründung `REASON_DIRECTION` und steht hinter denen mit passender Richtung – ausgeschlossen wird er nie (eine unbekannte
  Art oder ein Betrag, der keine Zahl ist, „passt“). Reihenfolge nach `(linked, mismatch, −Punkte, Abstand, ID)`: eine Buchung, die schon einen Beleg der
  Art `receipt` trägt (`linked`), immer zuletzt, innerhalb davon die mit unpassender Richtung hinter denen mit passender; **höchstens 5**
  (`MAX_SUGGESTIONS`) nach dieser Sortierung (`Suggestion.mismatch` dient nur der Reihenfolge, nicht der JSON-Form).
  `GET /receipts/suggestions?id=<n>` (`_SEARCH_PATHS`: rein lesend, Herkunft geprüft, `no-store`, ID über `_id_param`, unbekannter Beleg 404, ein
  unerwarteter Fehler 500 mit festem Satz) liefert `{suggestions: [{booking, reasons, linked}], empty}` in der Form eines Suchtreffers
  (`receipt_links.suggestions_payload`); `empty` ist der Satz für die leere Liste – EINE Funktion für Route und Seite
  (`pages_receipts.suggestions_empty_text`: verknüpft → `LINKED_TEXT`, kein Brutto oder keine Lesung → `NO_AMOUNT_TEXT`, sonst `NO_SUGGESTIONS_TEXT`), das
  Skript übernimmt ihn vor dem Zeichnen (`RECEIPT_SUGGEST.empty`). Dieselben Daten stehen im Skriptblock der Seite des Belegs (`RECEIPT_SUGGEST`, nur über `script_json`). Den
  Bereich „Vorschläge“ gibt es nur zu einer Lesung `ok` (`pages_receipts._suggestions_area`); scheitert die Abfrage dort, bleibt die Seite stehen
  (leere Liste, der Fehler im Server-Log). **„Zuordnen“ ist der bestehende Weg `POST /bookings/link-document` (*Belege verknüpfen*) – die Regeln der
  Beleg-Nr. gelten unverändert;** danach und nach dem Lösen holt das Skript die Vorschläge neu. Bekannte Grenzen: gesucht wird nur der Betrag auf den
  Cent (Skonto, Teilzahlung und Sammelüberweisung finden sich nicht) und nur in Euro (`docs/aufgaben.md`)
- **Tests** (nur mit erfundenen Belegen, Namen und Beträgen): `tests/test_receipt_reading_einvoice.py` (E-Rechnung, Grenzen der Eingabe, `Reading`),
  `tests/test_receipt_reading_text.py` (Textregeln, `pdf_text`, Kindprozess), `tests/test_document_readings_schema_2006.py` (Migration und
  DB-Schicht), `tests/test_receipt_read_route.py` (Route, Spalte, Kasten), `tests/test_receipt_matching.py` (Rangfolge, Kandidaten),
  `tests/test_receipt_suggestions_route.py` (Route und Bereich „Vorschläge“)

### Arbeitszeiten und Fahrtenbuch (seit 2.3.12)
- **Arbeitszeit rechnet in Minuten** (`server/pages_worktime.py`: `clock_minutes`, `compute_minutes`, `crosses_midnight`,
  `format_hours`): Uhrzeiten gelten nur als `HH:MM` aus ASCII-Ziffern (0–23, 0–59), sonst zählt der Eintrag 0 Minuten und wird
  beim Speichern abgelehnt („Bitte die Uhrzeit als HH:MM angeben.“). **Ende vor Beginn = Folgetag** (+ 24 h; Ende = Beginn sind
  0 Minuten); der Eintrag zählt ganz zu seinem Datum und trägt in Liste und PDF „(+1)“. **Summen entstehen aus Minuten, nie aus
  den gerundeten Stunden der Zeilen** (`compute_hours` ist nur die Zeilenanzeige); „Arbeitstage“ sind Kalendertage mit Arbeit
  über 0 Minuten – auf der Seite und im Stundenzettel gleich. Die Überlappungsprüfung (`_wt_has_overlap`) rechnet auf der
  Zeitachse des Eintragsdatums (ein Eintrag über Mitternacht reicht dort über 24:00 hinaus), die Nachbartage prüft sie nicht.
  Die Vorschau im Formular (`wtUpdateHours`) rechnet dieselbe Regel und nennt „Ende am Folgetag“
- **Fahrtenbuch bleibt locker** (Entscheidung des Owners): einzelne Fahrten, ohne Tachostand, in beliebiger Reihenfolge
  nachgetragen – lückenlos muss es nur für die Fahrtenbuchmethode sein. **Beim Speichern wird nie ein Anschluss an die
  Vorfahrt geprüft, weder als Fehler noch als Hinweis – nicht einführen.** Abgelehnt wird nur Unmögliches, an EINER Stelle
  (`handlers._trip_fields`, reine Funktion für Anlegen und Ändern): Fahrer/Start-Datum/Ziel fehlen, Datum kein ISO-Datum,
  Ende vor Start, Uhrzeit nicht `HH:MM`, Kilometer keine ganze Zahl von 0 bis 9 999 999, Tacho Ende unter Tacho Start,
  ungültiger Beleg, Text über 500 Zeichen. Die Meldung geht als `?error=` auf die Seite (`script_json` + `appMsg`), nichts
  wird gespeichert; das Formular prüft dasselbe vor dem Absenden (`trValidate`), der Server bleibt maßgeblich.
  `insert_trip`/`update_trip` melden einen Fehlschlag (`None`/`False`) – kein verschluckter Fehler. **Lückenprüfung nur auf
  Wunsch** (Kasten unter der Liste, GET `/trips?…&check_vehicle=&check_year=`, rein lesend): `db.trips.logbook_gaps`
  (reine Funktion) über alle Fahrten eines Fahrzeugs im Kalenderjahr, alle Fahrer, Fahrzeug nach `strip()` + `casefold()`.
  Lücke = Fahrt mit Tacho Start, deren nächster Vorgänger mit Tacho Ende einen anderen Stand hat; Fahrten ganz ohne
  Tachostand dazwischen werden gezählt (`between`), nach einem Start ohne Ende ist der Stand unbekannt (kein Vorgänger –
  dieselben Kilometer stehen nie in zwei Einträgen); höchstens 100 Einträge plus Zahl. Der Hinweistext zur
  Fahrtenbuchmethode steht fest auf der Seite (keine Steuerberatung). Eine gespeicherte Einstellung je Fahrzeug gibt es
  nicht – Fahrzeuge sind Freitext an der Fahrt, eine Stammliste wäre eine Schemaänderung
- **„Als neu kopieren“ (seit 2.5.3, Arbeitszeiten und Fahrten, wie bei der Rechnung):** Der Knopf steht nur an einem geladenen
  Eintrag und **speichert nichts** (`type="button"`, `wtCopyAsNew`/`trCopyAsNew`): Er leert die ID im Formular, stellt es auf
  „neu“ und setzt das Datum auf heute; angelegt wird erst mit „Speichern“ über `/worktime/add` bzw. `/trips/add` mit allen
  Prüfungen. Arbeitszeit: alle Angaben bleiben, die Vorlage des letzten Eintrags füllt eine Kopie nicht auf (`wtIsCopy`).
  Fahrt: Strecke, Fahrzeug, Grund, Uhrzeiten und Kilometer bleiben, eine mehrtägige behält ihre Dauer; Tachostände und Beleg
  bleiben leer (sie gehören zu der einen Fahrt). Das frühere „Als neu speichern“ der Fahrten (legte sofort an) gibt es nicht
  mehr. `tests/test_copy_as_new.py`
- **Stundenzettel-PDF** (`export/pdf_worktime.py`): mehrseitig über `build_multi_page_pdf`; `page_layout(row_count)` verteilt die
  Zeilen – keine Datenzeile unter `BOTTOM_Y`, Summe und Unterschriften zusammen auf der letzten Seite (notfalls eine eigene
  Seite ohne Datenzeilen), „Seite n von m“ nur bei mehr als einer Seite. Passt alles auf eine Seite, ist das Layout das bisherige.
  **Info-Spalte (seit 2.5.3):** Art des Tages, auf Wunsch der Arbeitsort eines Arbeitstags (`with_location`), Pausenzeiten, auf
  Wunsch die Notiz (`with_notes`), mit „ · “ verbunden. Die Seite hat dafür zwei Häkchen neben dem PDF-Knopf („Mit Notiz“, „Mit
  Arbeitsort“), **beide beim Laden an**; die Adresse nennt immer beide (`notes`, `location`: nur `1`/`true`/`on` schalten ein,
  ohne Angabe druckt der Server keines von beiden). Der Text **bricht in der Spalte um** (`info_lines`: am Wortrand, ein
  überbreites Wort mitten im Wort, Leerraum und Zeilenumbrüche als ein Leerzeichen) und endet nach `MAX_INFO_LINES` = 8 Zeilen
  mit „…“ – nie still abgeschnitten. Gemessen wird der Text, **wie er gedruckt wird** (`printed_text`: WinAnsi-Ersetzungen),
  mit den Breiten der eingebetteten Schrift (`info_width`, nicht die Helvetica-Tabelle – dort fehlen breite Zeichen), und
  **die Laufzeit hängt nur von den gedruckten Zeilen ab**: Jedes Zeichen wird höchstens zweimal gemessen, nach neun Zeilen
  hört das Lesen auf. Notiz, Pausentext und Art sind unbegrenzte Felder – nie wieder je Teilstück den ganzen Rest messen
  (ein Wort mit 8000 Zeichen kostete so 49 s). `page_layout` nimmt die Höhen der Zeilen in Textzeilen (oder wie bisher eine Zahl): Kein
  Eintrag wird über zwei Seiten geteilt, keine Textzeile steht unter `BOTTOM_Y` (`tests/test_worktime_pdf_info.py`)
- **Kundenfilter und leere Tage (seit 2.5.4, Arbeitszeiten-Seite und Stundenzettel):** Wer je Kunde abrechnet, braucht einen
  Zettel je Kunde; wer als Mitarbeiter seine Zeit nachweist, den Zettel über alles. **Die Regel steht an EINER Stelle**
  (`server/pages_worktime.py`): `customer_filter` liest den Wert streng (`''` = alle, `'none'` = ohne Kunde, sonst eine positive
  ASCII-Ganzzahl in 64 Bit, alles andere `None` – **auch Leerraum**: `customer=%20` ist kein leerer Wert), `entry_matches_customer` entscheidet je Eintrag, `customer_query` baut
  `&customer=…` für Adressen – Seite und PDF nehmen dieselbe. Zum Zettel des Kunden gehören seine Einträge **und die Einträge
  einer anderen Art als „Arbeit“ ohne Kunden** (Urlaub, Krank, Feiertag stehen auf jedem Kundenzettel – nur die Arten des
  Formulars, `KIND_LABELS`; eine unbekannte Art aus Altbestand oder SQL-Konsole ist keine Abwesenheit); Arbeitseinträge anderer
  Kunden und Arbeitseinträge ohne Kunde nie, der Urlaub eines anderen Kunden auch nicht. „ohne Kunde“ nimmt alle Einträge ohne
  Kunden, kein Filter alles. **Ungültig heißt auf der Seite „nicht angegeben“** (`GET /worktime`, `/worktime/edit`, Weiterleitung
  nach dem Löschen: der Filter fällt weg, nichts bricht), **beim PDF 400** „Ungültiger Kunde.“ (auch eine ID ohne Kontakt): Ein
  Zettel für alle darf nie entstehen, weil ein Kundenwert unlesbar war. Die Seite hat die Auswahl „Kunde“ (Alle; die Kunden mit
  Einträgen im Zeitraum bei dieser Person, alphabetisch ohne Rücksicht auf die Schreibweise, dazu der gewählte – eine unbekannte
  ID bleibt als „Kunde n (unbekannt)“ gewählt, der Filter fällt nie still auf „Alle“ –; „ohne Kunde“). Liste und Summe zeigen nur
  die passenden Einträge, die leeren Tage der Monatsansicht bleiben immer stehen. Der Filter bleibt über Speichern (verstecktes
  Feld `customer` – nicht zu verwechseln mit `customer_id`, dem Kunden des Eintrags), Löschen, Zeitraumwahl und PDF (`pdfBase`)
  erhalten; die Personenauswahl setzt ihn zurück; ein neuer Eintrag bekommt den gefilterten Kunden vorbelegt
  (`WT_DATA.filterCustomer`, `wtClear`). **Der Stundenzettel** (`generate_worktime_pdf(…, customer, with_empty_days)`) filtert vor
  allem anderen (Zeilen, Summe, Arbeitstage) und nennt den Kunden in einer vierten Kopfzeile bei y = 765 („Kunde: …“, bei „ohne
  Kunde“ so; mit „…“ auf `CUSTOMER_LINE_WIDTH` = 380 pt gekürzt, damit sie nicht ins Logo läuft – `customer_line`, höchstens die
  ersten 200 Zeichen), im rechten Unterschriftenblock (bei einem Kundenfilter dieser Kunde, auch ohne Arbeitseintrag im Zeitraum;
  ohne Filter wie bisher der eine Kunde der Einträge; bei „ohne Kunde“ keiner) und im Dateinamen
  (`Stundenzettel_<Person>_<Kunde>_<Zeitraum>.pdf`, „ohne Kunde“ = `ohne-Kunde`; je Name höchstens `FILE_PART_LENGTH` = 80
  Zeichen – ein Kontaktname hat keine Längengrenze). **„Mit leeren Tagen“** (Häkchen, beim Laden an;
  Adresse `empty`: nur `0`/`false`/`off` schalten aus, sonst an): Der Monatszettel listet dann auch die Tage ohne Eintrag, sonst
  nur die Tage mit Einträgen; über mehrere Monate ändert sich nichts. Ohne Filter bleibt jeder Zettel, wie er war
  (`tests/test_worktime_customer_filter.py`). **Die Notiz eines Eintrags hat höchstens `NOTE_MAX_LENGTH` = 300 Zeichen**
  (Entscheidung des Owners): Das Formularfeld trägt `maxlength` aus der Konstante; der Server lehnt beim Anlegen und Ändern (EINE
  Stelle: `handlers._wt_refusal`) eine längere Notiz ab – nichts gespeichert, 303 mit `?error=` und dem festen Satz „Die Notiz
  ist zu lang (höchstens 300 Zeichen).“, nie still gekürzt, ohne Teile der Notiz. **Gemessen und gespeichert wird ohne Leerraum
  am Rand** (`_wt_parse`: `strip()`) – sonst stünde hinter 300 Zeichen beliebig viel Leerraum in der Datenbank. Ebenso abgelehnt:
  eine **Art, die das Formular nicht kennt** („Bitte eine gültige Art wählen.“; sie gälte überall als „keine Arbeit“). Das Feld
  `customer_id` liest nur ASCII-Ziffern, alles andere heißt „kein Kunde“. Reihenfolge der Prüfungen: Uhrzeit, Art, Notiz,
  Überschneidung. Die Art aus der Datenbank steht auf der Seite nur maskiert. **Der Bestand bleibt:** Längere Notizen in der Datenbank werden weder
  gekürzt noch migriert (die Datenbankschicht begrenzt nichts), das PDF bricht sie wie bisher um und endet nach acht Zeilen mit
  „…“; wird so ein Eintrag geändert, greift die Prüfung (`tests/test_worktime_note_limit.py`)

### Stammdaten: Löschen, Standardkonten, Testdaten (seit 2.3.8)
- **Gelöscht wird nur, worauf nichts verweist:** `delete_contact`, `delete_article`, `delete_account`, `delete_asset_category`
  und `delete_receipt` prüfen die Verweise und löschen in EINER Transaktion (`BEGIN IMMEDIATE`); gibt es welche, werfen sie
  `db.DeleteRefused` (Klartext mit Zählern, Baustein `db.core.reference_texts`), und nichts ist gelöscht. Das gilt auch für
  Verweise, die das Schema per `ON DELETE CASCADE`/`SET NULL` mitnähme (Arbeitszeiten und Fahrten einer Person, der Beleg einer
  Fahrt): Eine Person löscht nie still ihr Fahrtenbuch. Kontakt: Rechnungen/Angebote (Kunde oder eigene Firma), Buchungen,
  Anlagen (Lieferant), zugeordnete Personen, Arbeitszeiten, Fahrten; Artikel: Positionen (mit dem Hinweis auf „inaktiv“, wenn er
  aktiv ist); Bankkonto: Buchungen, Rechnungen, Angebote – die Kasse nie; AfA-Kategorie: Anlagen; Beleg: Buchungen, Anlagen,
  Fahrten (`delete_receipt(receipt_id)` löscht genau EINEN Beleg über seine ID – die Belegnummer ist nicht eindeutig; Route
  `POST /receipts/delete?id=`, seine hochgeladene Datei geht mit, siehe *Belegdateien*; die Lesung der Datei, `DocumentReadings`, geht in derselben
  Transaktion mit und zählt nicht als Verweis, siehe *Belegerkennung*). Die Routen gehen über
  `_respond_after_delete` (`server/app.py`): 303 auf die Listenseite, die Meldung als
  Query-Parameter (`error` bei Artikeln, Bankkonten, Kontakten; `err` bei AfA-Kategorien und Belegen), angezeigt nur über
  `script_json` + `appMsg` – nie 500. Eine neue Verweisspalte auf eine dieser Tabellen gehört in die Prüfung ihrer
  Löschmethode. Jede Löschmethode schließt ihre Verbindung auch im Fehlerfall (try/rollback/finally) – sonst bleibt die
  Datenbank gesperrt
- **Testdaten lädt die Oberfläche nicht (seit 2.3.9):** Es gibt keinen Knopf und keine Route mehr (`POST /setup/load_testdata`
  antwortet wie ein unbekannter Pfad), weder unter *Sonstiges* noch in der Ersteinrichtung – Beispieldaten gibt es im
  Demo-Modus, der sie beim Start selbst lädt. `Database.load_test_seed_data` bleibt für Demo-Modus und Tests
- **Standardkonten ergänzt nur der Nutzer:** `_seed_chart_of_accounts` füllt allein eine leere Tabelle – der Kontenrahmen eines
  bestehenden Bestands bleibt beim Öffnen, wie er ist (ein Standardkonto lässt sich nach Entfernen des Hakens löschen und käme
  sonst bei jedem Start wieder). Was dem Bestand gegenüber den Seed-Dateien fehlt (nur Standardkonten; weder ID noch
  Rahmen + Nummer vergeben), nennt `Database.missing_standard_accounts()` (rein lesend, Hinweis auf der Seite des
  Kontenrahmens); angelegt wird es allein über `POST /masterdata/skr/add_standard` (`add_missing_standard_accounts`: eine
  Transaktion, `INSERT OR IGNORE`, kein UPDATE, keine `overrides`, keine privaten Konten ohne Standard-Haken). `?added=` wird nur
  als Zahl gelesen (0 bis `ADDED_MAX`), die Meldung ist fester Text

### Buchungssätze
- Bankbewegung = Zeile `BookingType='bank'`; ihre Buchungssätze sind Kinder
  (`entry`, `ParentBooking_ID`) und tragen selbst kein `Account_ID`
- **Die Form eines Satzes steht als Daten am Satz** (`Bookings.EntryForm`, seit 2.1.0;
  `db/matching.py`, Bankzeilen haben keine). Sechs Werte, je mit der Seite, auf die sich der
  Betrag bezieht: `purpose_first` (Zweckkonto vorne, Gegenkonto = Geldkonto oder leer; Betrag =
  Richtung auf dem Gegenkonto), `liquid_first` (Geldkonto vorne, v. a. WISO-Import; Richtung auf
  dem Konto), `transfer` (Umbuchung, beide Seiten kontiert, keine liquide; positiv = `COA` im
  Soll), `mirror_coa` / `mirror_counter` (Spiegel, beide Seiten Geldkonten; Betrag auf dem Konto
  bzw. dem Gegenkonto) und `mirror_legacy` (Spiegel des Bestands, Lesart unbekannt – gelesen wie
  vor 2.1.0: Gegenkonto-Seite, im Dashboard – zwischen eigenen Konten – als Übertrag mit dem Rohbetrag (eigenständig)
  bzw. ohne Lesung seines Betrags (Kind), in der Split-Summe über `is_bank_effective`).
  **Jeder Schreibweg setzt die Form** beim Schreiben, mit den Zahlungskonten von damals
  (`derive_entry_form`, `import_entry_form`; die App-Schreiber nennen sie ausdrücklich:
  Forderung und AfA `transfer`, Zahlungssätze `liquid_first`/`transfer`/`purpose_first`) und
  **leitet sie neu ab, wenn sich Konto oder Gegenkonto ändern** (`reform`: eine unveränderte Seite
  behält ihre Rolle, kippt die Seite des Betrags, wird er negiert – Soll/Haben bleibt; **mit dem Betrag
  dreht der Steuerbetrag**, sofern der Aufrufer ihn nicht ändert: `_form_after_accounts_change`, die eine
  Stelle für `update_booking_fields`, `update_booking`, `backfill_booking_fields` und den
  Tabellen-Export; `tests/test_entry_form_tax_sign.py`). Die Leser
  (EÜR, DATEV-Export, Dashboard, Split-Summe, Maske) **lesen die Form**; nur eine Zeile ohne Form
  (`EntryForm IS NULL`, per Roh-SQL/SQL-Konsole angelegt) wird mit den Zahlungskonten von jetzt
  eingeordnet (`read_entry_form`, `classify_entry`). Eine Änderung der Kontenliste ändert gebuchte
  Sätze also nicht mehr. Ausnahme, bewusst: Die Split-Summe zählt einen Satz ohne Spiegel-Form
  weiter über `is_bank_effective` (eigene Bankkonten) – die Form kennt die Zahlungskonten
  einschließlich 1460, nicht die eigenen Konten, und eine reine Formlesung hätte Bestand anders
  summiert
- Migration auf 2.1.0 (`_fill_entry_forms`): Der Bestand bekommt die Form, die 2.0.x ihm
  gab (Zahlungskonten von damals einschließlich 1000–1099, `_get_legacy_payment_coa_ids`; je
  Kontopaar einmal eingeordnet, gebündelt über die IDs geschrieben),
  Spiegel `mirror_legacy`; danach ist jede Auswertung identisch. **Vor jedem Migrationslauf einer
  bestehenden Datenbank** (`from_version < SCHEMA_VERSION`, Tabellen vorhanden) legt
  `initialize_database` eine Sicherung `backup/JJJJMMTT_vor_<APP_VERSION>_<Datei>` über die
  SQLite-Backup-API an: erst unter `<Name>.part`, nach der Prüfung per `os.replace` umbenannt, ein
  vorhandener Name wird nie überschrieben. Scheitert sie, öffnet die Datenbank nicht
  (`MigrationBackupError`), sie bleibt auf dem alten Stand. Die Umstellung selbst läuft in EINER
  Transaktion (`BEGIN` vor `ALTER TABLE`): Nach einem Abbruch ist die Datenbank so wie vorher. Es
  gibt kein Zurück auf 2.0.x außer über die Sicherung (Anleitung: README). Ein Spiegel des Bestands
  wird nie still umgedeutet: **Kein Import und keine Maske stellt `mirror_legacy` um** – die Importe
  erkennen ihn wieder und zählen ihn (`legacy_mirrors`), die Umstellung wäre eine Prüfliste, die den
  Nutzer entscheiden lässt (`docs/aufgaben.md`)
- Vorzeichen: Bei Zahlungen trägt der Betrag die Richtung der Geldbewegung
  (Einnahme +), egal auf welcher Seite das Zweckkonto steht. Bei Umbuchungen heißt
  positiv: `COA` im Soll, Gegenkonto im Haben. Fehlt die liquide Seite, bleibt es
  eine Zahlung (wie `is_bank_effective`). Spiegel: siehe Form
- DATEV (seit 2.3.0 dem Stapel nachgebildet, den WISO schreibt – gemessen an einem Datenbestand, keine Zusicherung, dass die
  Kanzlei ihn so annimmt): Das Soll/Haben-Kennzeichen bezieht sich auf das Feld „Konto“ und folgt der Form
  (`soll_side`: steht bei positivem Betrag das Feld „Konto“ im Soll, ist es bei positivem Betrag S).
  **Der BU-Schlüssel steht als Daten am Satz, wenn die Quelle ihn mitbringt** (`Bookings.DatevKey`, ganze Zahl 1–9999, Zeilenspalte 23;
  nur die Importe schreiben ihn, kein App-Schreibweg): Der Export gibt ihn unverändert aus – auch 490/100/121 ohne Satz; alles andere in der
  Spalte (0, über 9999, Text) gilt als keiner. **Er fällt (NULL), wenn sich Konto, Gegenkonto oder Steuersatz gegenüber dem gespeicherten Stand
  ändern** – `update_booking_fields` und `update_booking` über EINE Stelle (`_datev_key_stale`, Satz als Prozentzahl, im selben UPDATE),
  sofern der Aufrufer `datev_key` nicht selbst nennt, dazu der Tabellen-Export des Textimports, der ein leeres Konto aus der Kategorie füllt
  (dieselbe Stelle); ein unverändert zurückgeschickter Wert lässt ihn stehen (auch das Zuordnen im Editor, das ein leeres Gegenkonto füllt:
  dann fällt er). **Wo keiner gespeichert ist, leitet der Export ihn ab** (`db/datev_keys.py`, reine Funktionen) – nach
  der **Art des Zweckkontos** statt der Geldrichtung (Erlöskonto 4000–4999 und 6884–6892 [„Erlöse aus Verkäufen … (bei Buchverlust)“ im
  Aufwandsbereich, `DISPOSAL_REVENUE_RANGE`]: Umsatzsteuer, jedes andere Zweckkonto: Vorsteuer – auch bei einer
  Erstattung; Debitor und Spiegel: keiner; fehlt das Zweckkonto, zählt die Geldrichtung) und dem exakten Satz: Regelsatz 19 / ermäßigt 7 /
  „anderer Regelsatz“ 16 / „anderer ermäßigter“ 5 % → Vorsteuer 401/402/7/6, Umsatzsteuer 101/102/5/4, **vom 2020-07-01 bis 2020-12-31 tauschen
  die Plätze** (16/5/19/7 %); kein Satz, 0 % und jeder andere Satz → 0 (so auch die AfA, die Umbuchung „Aufwand an Anlagekonto“: Vorsteuer-Art,
  aber ohne Steuersatz). Die **Rechnungskette** (Forderung, „Wartekonto an Erlöskonto“,
  Gegenbuchung einer Stornorechnung: Umbuchung mit Debitor oder Wartekonto, `booking_kind`) trägt keinen Schlüssel, aber in Spalte 40 den Satz
  (`19,00`, an jeder Umsatzsteuer-Zeile mit Satz über 0); Leistungsdatum (114) nur an der Forderung (das der Rechnung, sonst das Buchungsdatum),
  Land (119) an der Kette (zwei Buchstaben groß, leer/D/Deutschland = DE, sonst leer; ohne Rechnung DE), Spalte 115 leer (`DateTax` setzt der
  Handler weiter). Belegfeld 1 ohne Leerzeichen, bis 36 Zeichen; im Buchungstext (Spalte 13) werden Zeilenumbrüche, Tabulatoren und andere
  Steuerzeichen vor dem Kürzen auf 60 Zeichen zu Leerzeichen. Der Vorlauf trägt Berater- und Mandantennummer der eigenen Firma
  (`CompanyDetails.DatevConsultantNo`/`DatevClientNo`, ganze Zahlen aus ASCII-Ziffern ohne führende Null: Berater 1001–9999999, Mandant 1–99999;
  im Kontaktformular der eigenen Firma gepflegt, `Database.get_datev_numbers`), sonst Platzhalter und ein Hinweis auf *Sonstiges*. Jeder Wert, der in den Stapel geht, ist streng geprüft –
  ein per SQL-Konsole gesetzter anderer Wert gilt als nicht vorhanden, nie roh ausgegeben. Die Schlüssel sind nicht gegen einen echten Stapel
  der Kanzlei geprüft (`docs/aufgaben.md`) – nicht als richtig voraussetzen. **Vollständigkeit (seit 2.3.4, `handle_datev_export`):** Ein
  Zeitraum über den Jahreswechsel wird abgelehnt (`DATEV_YEAR_REFUSED`: der Stapel kennt nur Tag und Monat, Wirtschaftsjahr =
  Kalenderjahr). **Überträge zwischen eigenen Konten werden gezählt, nicht gepaart** (`db/matching.py`: `transfer_legs` baut aus dem
  ganzen Bestand Beine – Bankbewegung mit Spiegel-Kind oder ganz ohne Kind, Richtung allein nach dem Vorzeichen der **Bankzeile**,
  `mirror_legacy` wird nie gedeutet –, eigenständige Spiegel mit bekannter Lesart und „anders kontierte“ Bankbewegungen (Kinder, aber
  kein Spiegel-Kind); `settle_transfers` ist die reine Rechnung, `Database.check_transfers` der Aufruf, O(n log n). Nicht teil nehmen
  Konten ohne oder mit doppelter SKR-Nummer und Zeilen, deren Datum nicht exakt `JJJJ-MM-TT` oder deren Betrag keine ganze Zahl ist).
  Gruppe = (Ausgangskonto, Eingangskonto, Betrag); Abschnitt = Elemente mit höchstens `TRANSFER_DAY_WINDOW` = 7 Tagen Abstand; Umfeld =
  Elemente der Gruppe bis `TRANSFER_NEIGHBOUR_DAYS` = 14 Tage vor und nach dem Abschnitt. **Die Eingangs-Sätze eines Abschnitts fallen
  aus dem Stapel** (alle oder keiner, kein `DateTax`), wenn jede Ausgangsbewegung ihren Satz trägt und es mindestens so viele Ausgänge
  wie Eingänge gibt (`a_m ≥ 1`, `b_m ≥ 1`, `a_m ≥ b_m + b_c`) und im Umfeld weder ein Ausgang ohne Satz noch ein eigenständiger Spiegel
  liegt – sonst bleibt jede Zeile stehen. Eine Bankbewegung ohne Satz gilt nur als Gegenseite eines Übertrags, wenn die überschüssigen
  Spiegel der Gegenseite im Umfeld – abzüglich der dort anders kontierten Bewegungen – für ALLE satzlosen ihrer Seite reichen.
  **Hinweise** (nie mit Text oder Empfänger): (1) Bankbewegungen im Zeitraum ohne Buchungssatz, die kein Übertrag deckt; (2) Überträge
  über die Zeitraumgrenze – weggelassene Eingangs-Sätze, deren Ausgangsseite außerhalb liegt, und umgekehrt Ausgangs-Sätze, deren
  weggelassene Eingangsseite außerhalb liegt (je Abschnitt gezählt: eine Serie gleich hoher Überträge löst ihn nur an einer wirklich
  überquerten Grenze aus); (3) Sätze, die einen Übertrag möglicherweise doppelt abbilden (nicht zusammengelegt, obwohl im Umfeld ein
  Ausgangs-Satz, eine anders kontierte Gegenbewegung oder ein eigenständiger Spiegel steht; mit dem ältesten Auslöser). Mit Hinweisen
  exportiert der Handler nur mit `confirm_export` = `1` und `confirmed_check` = Prüfsumme über Zeitraum und alle gemeldeten Einträge
  (ändert sich der Bestand, kommt der Hinweis neu; Muster wie beim Import der WISO-Datenbank in einen nicht leeren Bestand). Maßstab,
  per Aufzählung kleiner Bestände geprüft (`tests/test_datev_transfer_enumeration.py`, benannte Grenzen dort und in `DB_MODEL.md`): Bei
  vollständigen Auszügen und höchstens 14 Tagen zwischen den beiden Seiten fällt nie ein Übertrag weg und steht keiner doppelt, ohne
  genannt zu sein. Diese Regeln nicht lockern – lieber ein Hinweis zu viel. Bekannte Grenzen: mehr als 14 Tage Laufzeit, eine ferne
  Lücke sperrt eine ganze Serie (genannt), Stückelung (eine Bewegung gegen mehrere kleinere), Konten mit gleicher SKR-Nummer
- Zahlungskonten (`get_payment_coa_ids`): die SKR-Konten der eigenen Bank- und Kassenkonten –
  die Kasse der App (SKR04 1600) zählt wie ein Bankkonto über ihr eigenes Konto, nicht über
  einen Bereich – plus das Verrechnungskonto 1460 (`DEFAULT_LIQUID_ACCOUNTS`; Geldtransit: WISO
  bucht darüber aufgeteilte Zahlungen und Anlagen-Umbuchungen). Das ist die Menge, an der die
  Importe das Vorzeichen ausrichten und die Maske und `insert_booking` die Form **beim Schreiben**
  ableiten – und mit der eine Zeile ohne Form gelesen wird. **1000–1099 gehört nicht mehr dazu**
  (SKR04: Vorräte; früher der SKR03-Kassenbereich): Beide WISO-Importe melden Buchungen darauf
  (`stock_range_accounts`, Hinweis auf *Sonstiges*: Buchungen gegen diese Konten werden als
  Umbuchung gelesen und erscheinen nicht in der EÜR), und `negative_gross` zählt jeden Satz mit negativem
  Brutto und genau einer liquiden Seite (dort bestimmt die Geldbewegung das Vorzeichen – **festgeschrieben** nach der
  Messung an echten Daten: WISO schreibt den Brutto nie negativ, Konto = Soll, Umkehrungen stehen als vertauschte Konten;
  der Zähler ist der Wächter für Dateien außerhalb dieses Musters).
  Ob ein Satz in die EÜR kommt, entscheidet die
  Sektion in `get_euer_data` – Kind einer Bank- oder Kassenbewegung; eigenständig auf einer
  Kasse oder (ohne Kontofilter) auf 1460; Umbuchung zwischen Anlage- und Erfolgskonto –,
  nicht die Form und nicht die Kontenmenge; jede Buchung zählt dabei höchstens einmal
  (Schlüssel: ihre ID). Verknüpfung und Split-Summe rechnen mit den eigenen Bankkonten
  (`get_bank_coa_ids`); ein Spiegel mit bekannter Lesart wirkt in der Split-Summe nach seiner
  Position auf dem Konto der Bankbewegung (`bank_effect`); im Dashboard ist ein Spiegel zwischen eigenen
  Konten ein Übertrag (eigenständig mit dem Betrag aus Sicht der Kasse)
- **Dashboard** (`Database.get_dashboard`, seit 2.4.1; `get_dashboard_totals`/`-monthly` sind dünne Hüllen darum): Es folgt den
  Kontobewegungen und zerlegt **jede** gezählte Bewegung ohne Rest in vier Töpfe – Einnahme, Ausgabe, privat, Übertrag
  (`reporting.split_bank_movement`/`split_cash_entry`, reine Funktionen in Minor Units); `balance` ist ihre Summe. Gezählt wird
  wie vorher (`bank_count` und `unlinked_count` ändern sich nicht, der Saldo nur bei Werten, die allein die SQL-Konsole schreibt:
  ein Kassensatz in Spiegel-Form mit einem Privatkonto, Beträge mit Bruchteil oder als Text): die Bankbewegungen der gewählten Konten und die
  eigenständigen Sätze auf Kassenkonten (Konto der Kasse auf einer Seite, oder `Account_ID` = Kasse und kein AfA-Satz); 1460
  (außer als Konto der Kasse) und die AfA zählen nicht. EÜR, DATEV-Export und gespeicherte Daten ändern sich nicht.
  **Bankbewegung mit Kindern:** Die Wirkung eines Satzes ist `bank_effect` auf das Konto der Bewegung, wie in der Split-Summe.
  *privat* = Summe über die Kinder mit 2100–2199 auf einer Seite: bei einer Umbuchung (`transfer`) −Betrag (im Konto) bzw.
  +Betrag (im Gegenkonto), auf beiden Seiten 0 – auch nicht ganz kontiert –, bei jeder anderen Form außer den Spiegeln ihr
  **Betrag**, nicht ihre Wirkung (er trägt die Richtung der Geldbewegung; „2100 an 1460“ unter der Bewegung – WISO-Paar mit
  „1460 an Bank“, Kasse nicht auf 1460 – hat die Wirkung 0 und bliebe sonst Betriebsausgabe). *Übertrag* nur, wenn die Bewegung mindestens einen Spiegel zwischen eigenen Konten trägt (`is_mirror_row`, jede
  Spiegel-Form): Betrag minus Summe der Wirkungen aller übrigen Kinder; der Betrag des Spiegels wird nie gelesen, auch nicht
  bei `mirror_legacy`. Der Rest (Betrag − privat − Übertrag) geht nach dem Vorzeichen in Einnahme oder Ausgabe. Die Formel
  „Betrag − privat − Wirkung der übrigen Kinder“ ist falsch – sie zöge einen privaten Umbuchungssatz doppelt ab (−50 mit
  Spiegel −40, „6805 an Bank“ −10 und „2100 an 6805“ +10: Übertrag −40, privat −10, Ausgabe 0).
  **Bankbewegung ohne Kind:** Deckt die Übertragsrechnung des DATEV-Exports sie (`settle_transfers(…).covered`, über den
  ganzen Bestand; `db/matching.py` bleibt unberührt), ist sie ganz Übertrag, sonst nach dem Vorzeichen Einnahme oder
  Ausgabe; „offen“ (`unlinked_count`) bleibt sie in beiden Fällen. Die Rechnung läuft nur, wenn eine solche Bewegung zu
  zählen ist, und einmal je `get_dashboard` (die Seite ruft nur das auf). **Eigenständiger Satz auf der Kasse** (Betrag aus
  Sicht der Kasse, `_cash_amount_sql`): ein Spiegel zwischen eigenen Konten (`is_mirror_row`, jede Form; `mirror_legacy` und
  Zeilen ohne Form mit dem Rohbetrag) ist Übertrag, 2100–2199 auf einer Seite privat, alles andere nach dem Vorzeichen.
  **Kontofilter:** Ein Übertrag auf ein nicht gewähltes eigenes Konto bleibt ein Übertrag und steht dann netto unter
  „Überträge“. Die Kassenseite eines Spiegel-Kindes unter einer Bankbewegung zählt weiter nicht (sie stünde doppelt da,
  sobald die Kasse die Bewegung selbst führt); ebenso bekannte Grenzen: der unkontierte Rest neben einem Spiegel steckt im
  Übertrag (auch ein Satz über 1460 ohne Wirkung auf das Konto), Konten ohne oder mit doppelter SKR-Nummer nehmen an der
  Deckung nicht teil, ein eigenständiger `mirror_legacy`-Spiegel auf der Kasse deckt die satzlose Bankseite nicht (er wird nie
  gedeutet: Ausgabe und Übertrag statt zweimal Übertrag), und die Deckung setzt wie beim DATEV-Export vollständige Auszüge
  voraus – fehlt die echte Gegenseite eines kontierten Übertrags im Bestand, kann eine gleich hohe Bewegung ohne Satz auf dem
  Zielkonto binnen 14 Tagen als Übertrag zählen, bis sie kontiert ist (`docs/aufgaben.md`). Beträge aus der
  Datenbank nur über `_minor_or_zero` (Text, NULL, Kommazahl ab 2^63: 0), ein Datum, das SQLite nicht liest, steht nur in den Summen – nie ein
  Fehler. Seite: Die Zeile „Überträge zwischen eigenen Konten“ (`.dashTransfers`) steht in der Übersicht nur bei einem Wert
  ≠ 0 (auf den Cent), im Monats-Tooltip nur bei ≠ 0, ohne Balken; Werte maskiert. `tests/test_dashboard_split.py` pinnt die
  Zerlegung (Regeltabelle, Eigenschaftsprüfung, feste Werte der Testwelt); der Maßstab von 2.0.8
  (`entry_form_golden_2_0_8.json`) gilt für das Dashboard nur über Saldo, Anzahl und Monatssummen
- **Betriebswirtschaftliche Auswertung (seit 2.5.14, `db/bwa.py`, `server/pages_bwa.py`, `export/pdf_bwa.py`; `GET /bwa`, `GET /bwa/pdf`):** eine
  reine Umgruppierung von `get_euer_data` ohne Kontofilter – sie übernimmt die Zahlen der EÜR unverändert und hat keine eigene Lesart der
  Buchungen. Einnahme, Ausgabe und „nicht im Ergebnis“ entscheidet EINE Funktion, `reporting.euer_section`, die auch die EÜR-Seite des
  Dashboards nutzt (`tests/test_bwa.py` hält die frühere Regel dieser Seite für alle Kontonummern fest). Die Gruppen stehen als Daten in
  `db/bwa.py` (`RANGES`, `EXCEPTIONS`, `group_of`); vom Nutzer pflegbare Gruppen gibt es nicht (das wäre eine Schemaänderung). Ein Konto ohne
  Gruppe steht unter „Nicht zugeordnet“ und zählt im Ergebnis wie in der EÜR – nie still in einer anderen Gruppe. Die Konten 3800–3849 außer 3806
  gehen je Konto und Zeitraum nach dem Vorzeichen ihres Saldos zu Erstattungen (> 0, unter den Betriebseinnahmen) bzw. Zahlungen. Invarianten
  (Test, auf den Minor-Wert, Testwelt und Demobestand): Ergebnis = EÜR-Ergebnis; Betriebseinnahmen − Erstattungen = Summe der EÜR-Einnahmen;
  Betriebsausgaben − Erstattungen = −Summe der EÜR-Ausgaben. Gerechnet wird nur in Minor Units, Prozente entstehen erst bei der Ausgabe: Ein
  Anteil bleibt leer bei einer Bezugsgröße ≤ 0, die Veränderung in % bei einem Vorjahreswert von 0 (und bei einem Quotienten, der den Decimal-Kontext sprengt). Gerundet wird erst
  bei der Anzeige: Trägt ein Bestand Beträge mit Bruchteilen eines Cents, kann eine gedruckte Summe um wenige Cent von der Summe der gedruckten
  Zeilen abweichen (wie in der EÜR) – nicht glätten, die Gleichheit mit der EÜR geht vor. Zwei Konten gleicher Nummer (verschiedene
  Kontenrahmen) stehen im Kontennachweis als eine Zeile unter dem zuerst gefundenen Namen. **Zeitraum und Darstellung:** der Monat und
  aufgelaufen ab 1. Januar. Die Seite nimmt den gemeinsamen Zeitraum der Seiten (`from`/`to` in der Adresse, sonst das Cookie, sonst das laufende
  Jahr – `resolve_period`; das Cookie setzt die Route wie bei jeder Seite mit Zeitraum) und leitet ihren Monat daraus ab (`period.period_month`, siehe
  *Dritte Menüzeile*); `year` und `month` liest sie nicht mehr (eine Adresse der alten Form ist eine Adresse ohne Zeitraum), einen Hinweis „ungültig“
  gibt es nicht – ein ungültiger Zeitraum wird wie überall verworfen. Das PDF liest `year`, `month` und `variant` streng (`parse_year`, `parse_month`,
  `parse_variant`; ein doppelter Schlüssel ist ungültig, 400 bei einem Fehler) und setzt kein Cookie; die Darstellung ist der Vergleich mit dem
  Vorjahr (Standard, auf der Seite auch bei ungültiger Angabe) oder `variant=jahr` (Monat und Jahr mit Anteilen), ihre Auswahl steht im Inhalt (kleines
  GET-Formular mit den Feldern `from`/`to`, kein „Anzeigen“-Knopf). Das PDF ist A4 quer, mehrseitig
  und landet nie auf der Platte (Download über `appDownload`; Dateiname `BWA_JJJJ-MM.pdf`, mit Anteilen `…_Anteile.pdf`). `/bwa` steht in der
  Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/bwa/pdf` nicht. Fester Hinweis: keine Steuerberatung.
  **CSV (seit 2.6.8, `export/csv_bwa.py`, `GET /bwa/csv`):** dieselben Zeilen und Zellen wie die Seite (`cells`), auf Wunsch mit Konten; UTF-8 mit BOM, Semikolon,
  Dezimalkomma ohne Tausenderpunkte, Prozentwerte ohne Zeichen; die Adresse wird streng gelesen wie beim PDF, nichts landet auf der Platte. Texte aus der
  Datenbank gehen nur über `csv_table.text_cell` (Steuerzeichen weg, höchstens 200 Zeichen, ein `'` vor `=`, `+`, `-`, `@` – auch hinter
  jedem Komma des Textes, falls jemand die Datei mit dem Komma als Trenner öffnet, und auch vor den breiten Schreibweisen dieser Zeichen (verglichen wird
  die Grundform, NFKC); sonst führte eine Tabellenkalkulation einen Kontonamen als Formel aus; nicht aufweichen).
  `tests/test_bwa.py`, `tests/test_bwa_pdf.py`, `tests/test_report_csv.py`
- **EÜR-Ansicht (seit 2.5.16, `db/euer.py`, `server/pages_euer.py`, `export/pdf_euer.py`; `GET /euer`, `GET /euer/pdf`):** EINE Aufbereitung für den
  Kasten der EÜR im Dashboard und die Seite (`db.euer.euer_statement`: die Zeilen von `get_euer_data` nach `reporting.euer_section` in Einnahmen,
  Ausgaben und „Sonstige Ausgaben“; Summen aus Minor Units, nie als Summe von `float`; die echte und die virtuelle 3806 bleiben zwei Zeilen;
  Ergebnis = Einnahmen + Ausgaben, die „Sonstigen Ausgaben“ zählen nicht mit). Der Kasten im Dashboard bleibt **zeichengleich** – bis auf die eine
  Zeile mit dem Link „zur Auswertung“ unter der Überschrift (mit dem Zusatz „(dort ohne Kontofilter)“, wenn dort Konten gewählt sind): SHA-256-Schnappschüsse
  am Stand 2.5.14 (`tests/test_euer_view.py`); ändert sich ein Zeichen, ist der Code zu ändern, nie der Schnappschuss. Die bekannten Abweichungen
  von 2.5.14: Ein Ergebnis von genau 0 ist grün (der Kasten rechnete die Summen als `float`; ein Rest von −5,55e-17 färbte das „0,00 €“ rot), und
  bei Summen ab rund 6·10¹¹ € kann der letzte Cent anders – nämlich richtig – gerundet sein (die `float`-Summe verlor dort Stellen); die Zeilen
  selbst sind immer gleich. Ein leerer Wert in der Adresse (`from=`) gilt als nicht angegeben (`parse_qs` lässt ihn fallen, wie bei `/bwa`). Die Seite
  hat **keinen Kontofilter** (mit Filter fielen das Verrechnungskonto 1460 und die AfA weg, es wäre keine EÜR mehr). Die Seite nimmt den gemeinsamen
  Zeitraum der Seiten (`from`, `to` in der Adresse, sonst das Cookie, sonst das laufende Jahr – `resolve_period`): Ein ungültiger Wert wird wie überall
  verworfen, ohne Hinweis, ein Paar mit `from` nach `to` getauscht (`valid_period`), das Cookie setzt die Route wie bei jeder Seite mit Zeitraum; ihre
  Menüzeile ist `period_filter_widget` (die fünf Jahresknöpfe rechnen aus dem `today`, das die Route übergibt). Das PDF liest den Zeitraum weiter streng
  (`period.parse_iso_date`): beide sind nötig, `from` liegt nicht nach `to` – **kein stiller Tausch** –, ein doppelter Schlüssel ist ungültig, 400
  „Ungültiger Zeitraum.“, kein Cookie. Das PDF ist A4 hoch, mehrseitig und landet nie auf der Platte
  (Download über `appDownload`; Dateiname `EUER_JJJJ.pdf` für ein volles Kalenderjahr, sonst `EUER_von_bis.pdf`); seine Bausteine (Text bereinigen,
  kürzen, umbrechen, Zelle, Linie, Seitenverteilung) stehen in `export/pdf_table.py` und gelten auch für `pdf_bwa`, dessen Ausgabe dabei dieselbe blieb
  (Schnappschuss in `tests/test_pdf_table.py`). Fester Hinweis: kein amtliches Formular, keine Steuerberatung. **Untermenü der Auswertungen:** `REPORT_PAGES`
  und `reports_submenu` in `server/pages_bwa.py` (seit 2.6.3 mit der Liquidität als viertem Eintrag); jede Seite darunter ruft `Header1('bwa')` auf (der Menüpunkt bleibt markiert). `/euer` steht in der
  Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/euer/pdf` nicht. `tests/test_euer_view.py`, `tests/test_pdf_table.py`
- **Summen- und Saldenliste (seit 2.5.17, `db/susa.py`, `Database.get_susa`, `server/pages_susa.py`, `export/pdf_susa.py`; `GET /susa`, `GET /susa/pdf`):** je Konto
  Anfangssaldo, Soll und Haben im Monat und aufgelaufen seit dem 1. Januar, Endsaldo (`1.234,56 S`/`H`). **Sie liest dieselben Zeilen wie der Export für das Steuerbüro und
  hat keine eigene Lesart:** `accounts.LEDGER_ROWS_WHERE` ist das `WHERE` von `fetch_bookings_range` (alle Sätze außer den Bankbewegungen bis Monatsende, die Vorjahre bilden den
  Anfangssaldo), Form und Soll-Seite kommen aus `matching.ledger_form`/`coa_in_soll` – dieselben Funktionen, die `export_to_datev` ruft; die Auslagerung ließ den Export **Byte für Byte**
  gleich (SHA-256-Schnappschüsse in `tests/test_susa.py`, am Stand 2.5.16 vor der Auslagerung genommen: ändert sich ein Byte, ist der Code zu ändern, nie der Schnappschuss) –,
  die Art aus `datev_keys.booking_kind`, die Wartekonten aus `get_waiting_account_numbers`; dazu fehlen die Eingangs-Sätze gepaarter Überträge (`settle_transfers(…).omitted`: der Ausgangs-Satz bucht
  beide Bankseiten) und die gedeckten Bankbewegungen ohne Satz (`.covered`). Ein Test hält für JEDE Zeile der Testwelt und des Demobestands fest, dass die Liste dieselbe Seite bucht wie die Zeile des
  Exports (Konto, Gegenkonto, S/H, Betrag). **Buchungsregel** (`entry_postings`, rein, in Minor Units): Betrag ohne Vorzeichen, Soll/Haben nach der Form (`purpose_first`: positiv Gegenkonto im Soll,
  `liquid_first`/`transfer`/`mirror_coa`: positiv Konto im Soll, `mirror_counter`: positiv Gegenkonto im Soll); `mirror_legacy` bucht nie (Lesart unbekannt – wie beim Abgleich nie gedeutet, Hinweis); der
  **gespeicherte** Steuerbetrag wird aus der Seite des Zweckkontos herausgerechnet (nichts nachgerechnet) und steht auf 1401/1406 (Vorsteuer) bzw. 3801/3806 (Umsatzsteuer; 16 % und 5 % auf den Konten von 19 % und 7 %,
  `TAX_ACCOUNTS`; ein anderer Satz auf „Steuer ohne Steuerkonto“; ein Steuerkonto, das der Kontenrahmen nicht führt, mit dem Zusatz „(nicht im Kontenrahmen)“ – der Standard-Kontenrahmen führt 3801 seit 2.6.1,
  ein älterer Bestand bekommt es über „fehlende Standardkonten“ und trägt bis dahin den Zusatz), die Seite bestimmt `tax_side` nach der Art; die
  Forderung einer Rechnung und die Gegenbuchung der Stornorechnung (Debitor auf einer Seite) werden nie zerlegt. **Kontenklassen und Vortrag stehen an EINER Stelle** (`GROUPS`, `group_key`, `is_carried`):
  Bestandskonten und die Wartekonten 4340/4345/4405 (eigene Gruppe „Offene Posten“) tragen vor, Erfolgskonten 4000–7999 beginnen bei 0 – ihre Summe der Vorjahre steht auf dem virtuellen Konto „Ergebnis der
  Vorjahre (rechnerisch)“ beim Eigenkapital; Privat- und Steuerkonten werden mangels Abschluss vorgetragen, eine Eröffnungsbilanz gibt es nicht (ein Konto ohne Buchungen vor dem ersten Kontoauszug beginnt bei 0).
  **Ausgleich:** Jede Buchung schreibt gleich viel ins Soll wie ins Haben, was fehlt, steht auf dem virtuellen Konto „Nicht kontiert“ (eine Seite ohne Konto oder Nummer, die Gegenseite einer Bankbewegung ohne Satz, die
  kein Übertrag deckt): Summe Soll = Summe Haben je Spalte, Summe der Anfangs- und der Endsalden 0. Ein Satz unter einer Bankbewegung ohne Geldseite (`purpose_first` ohne Gegenkonto, `liquid_first` ohne Konto)
  zählt für das Konto der Bankbewegung (Hinweis; ohne Nummer: „Nicht kontiert“), eine Zeile ohne Form wird wie im Export mit den Zahlungskonten von heute gelesen (Hinweis). Was sich nicht buchen lässt
  (`mirror_legacy`, ein Betrag, der keine ganze Zahl bis 10^15 Minor Units ist, ein Datum, das kein Kalendertag ist, eine Zeile ohne Buchungsart, ein Satz mit Betrag 0 und Steuerbetrag), steht nur in den **Hinweisen** (`NOTICE_TEXTS`, Einzahl und Mehrzahl) – nie ein Fehler, nie still; auch die
  Bankbewegungen mit unstimmigen Sätzen (`unbalanced`) rechnet `get_susa` mit den lesbaren Beträgen selbst (`_susa_unbalanced`, dieselbe Regel `bank_effect` und Auswahl wie `find_unbalanced_splits`, das bei einem Betrag,
  der Text ist, mit einem Fehler abbricht). **Gültiges Datum** ist ein Kalendertag als Text `JJJJ-MM-TT` ab dem Jahr 0001 (`_SUSA_DATE_OK`: `typeof = 'text'`, das Muster und `x IS date(x)` – das Muster hält die Form unabhängig von der SQLite-Version fest; SQLite rückt `2026-02-30` auf einen echten Tag, kennt `Dezember` nicht und liest die Zahl
  `20260305` nicht als Text): EINE Bedingung für die gebuchten Sätze und die Bankbewegungen ohne Satz. `bad_date` zählt jede Zeile mit Buchungsart und ungültigem Datum, auch eine Bankbewegung, **ohne Fenster** (sie fehlt in jedem Monat, auch im
  Vortrag), `unknown_type` die Zeilen ohne Buchungsart (`BookingType` NULL: die Bedingung `!= 'bank'` des Exports trifft sie nie, er liest sie so wenig wie die Liste), `not_readable` nur Zeilen mit gültigem Datum bis zum Monatsende – jede Zeile steht in
  genau einem dieser Hinweise (Reihenfolge: Buchungsart, Datum, Betrag); eine andere Buchungsart als `entry` und `bank` liest der Export wie einen Satz, und so bucht sie die Liste. Ein Satz mit **Betrag 0 und Steuerbetrag** (der Bank-Editor lässt ihn zu)
  bucht nichts – ohne Betrag gibt es keine Seite –, `zero_amount_tax` nennt ihn mit der Summe der Steuer. **Abstimmungsproben** (Pflicht-Tests, auf den Minor-Wert): (a) Summe Soll = Summe Haben, Anfangs- und Endsalden zusammen 0 – in jedem Bestand und jedem Monat, auch in
  Beständen aus Zufall (fester Seed); (b) jedes eigene Bankkonto (keine Kasse, eigene SKR-Nummer) hat als Endsaldo die Summe aller Bankbewegungen bis Monatsende (nach dem Datum als Text: Eine Bewegung, deren ungültiges Datum vor dem Monatsende sortiert, zählt mit,
  fehlt in der Liste und steht unter `bad_date`) – steht als „Abgleich mit den Bankbewegungen“ auf der Seite;
  es geht auf, wo jede Bewegung kontiert ist, jeder Übertrag beide Seiten am selben Tag trägt, alle Beträge lesbar sind, jeder Satz mit seiner Bankbewegung auf derselben Seite des Monatsendes liegt (sonst `child_other_period`: um den Satz kann der Abgleich abweichen – gezählt wird jeder solche Satz, auch einer ohne Wirkung auf das Bankkonto) und
  jeder Satz unter einer Bewegung auf das Konto der Bewegung bucht (sonst `child_other_account`: ein gebuchter Satz, dessen Konto oder Gegenkonto ein anderes eigenes Bank- oder Kassenkonto ist – ein Spiegel zwischen eigenen Konten zählt nicht, er
  berührt das andere Konto absichtlich; beide Hinweise nur gezählt, an der Buchung ändert sich nichts); (c) die Erfolgskonten gegen die EÜR (für jede Zeile eines EÜR-Abschnitts außer 3806 der Saldo der EÜR =
  Haben − Soll der Liste, die Umsatzsteuer der EÜR = 3801 plus 3806) – es geht auf, wo jeder Satz einer Bankbewegung oder der Kasse zugeordnet ist; Sätze ohne Bankbewegung, Umbuchungen ohne Zahlung und Status `resolved`
  zählt die Liste nach ihrem Datum, die EÜR nicht. **Laufzeit:** Die Sätze werden in SQL nach dem gruppiert, was ihre Lesart bestimmt (Form, Konten, Richtung des Betrags und der Steuer, Satz, Zeitraum, Konto der Bankbewegung), und je Gruppe
  einmal gebucht (`book_group`, die Regel ist linear) – nie wieder eine Zeile je Satz in Python; reicht eine Summe über 2^63 Minor Units hinaus (SQLite wirft `integer overflow`), rechnet `reporting._sum_groups` dieselbe Abfrage in Python (jeder andere
  Datenbankfehler geht weiter), und ein Name, der kein Text ist (Blob, NULL), wird als Text gelesen (`_name_text`). Die Seite nimmt den gemeinsamen Zeitraum (`from`/`to` in der Adresse, sonst das Cookie, sonst das laufende Jahr – `resolve_period`; das Cookie
  setzt die Route wie bei jeder Seite mit Zeitraum) und leitet ihren Monat daraus ab (`period.period_month`); `year` und `month` liest sie nicht mehr, einen
  Hinweis „ungültig“ gibt es nicht. Sie schreibt nichts in den Bestand; das PDF liest `year` und `month` streng (`parse_year`/`parse_month`, ein doppelter
  Schlüssel ist ungültig, 400) und setzt kein Cookie (A4 quer, mehrseitig, `Summen_und_Salden_JJJJ-MM.pdf`, landet nie auf der Platte). Texte ohne Marken- und Produktnamen; fester Hinweis: keine Steuerberatung, ohne Zusicherung der
  Richtigkeit. **Diese Regeln nicht still ändern.** `/susa` steht in der Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/susa/pdf` nicht.
  **CSV (seit 2.6.8, `export/csv_susa.py`, `GET /susa/csv`):** je Konto und Gruppe die sechs Werte immer als Zahl, Salden als Betrag mit eigenem Kennzeichen S/H, am Ende
  die Gesamtsumme; Hinweise und Bankabgleich stehen nur auf Seite und PDF.
  `tests/test_susa.py`, `tests/test_susa_pdf.py`, `tests/test_report_csv.py`
- **Liquiditätsrechnung (seit 2.6.3, `db/liquidity.py`, `Database.get_liquidity`, `server/pages_liquidity.py`, `export/pdf_liquidity.py`; `GET /liquiditaet`, `GET /liquiditaet/pdf`):** vom Ergebnis der
  BWA zur Veränderung der flüssigen Mittel – ein Monat und aufgelaufen seit dem 1. Januar, im Vorjahresvergleich (Standard, acht Wertspalten) oder als „Monat und Jahr“ (`variant=jahr`, zwei
  Spalten, **keine Anteile**: ein Anteil an Erlösen ergäbe bei Liquidität keinen Sinn). **Sie hat keine eigene Lesart der Buchungen:** Gerechnet wird nur mit den Zeilen von `get_euer_data` (ohne Kontofilter,
  eingeordnet von `euer_section`; je Spanne ein Aufruf mit allen Zeilen und – seit 2.6.4 – einer mit `only_asset_transfers=True`: nur die Zeilen aus den Umbuchungen
  zwischen Anlage- und Erfolgskonto, die kein Geld bewegen: die Sätze, die Sektion 3 der EÜR beisteuert, und die Umbuchungen dieser Art UNTER einer Bankbewegung (Sektion 1, `is_asset_transfer`: der Restbuchwert im Split eines
  Verkaufs, eine dort zugeordnete AfA – ihre Wirkung auf das Konto der Bewegung ist 0), jeder Satz mit dem, was er im vollen Lauf beisteuert; ein Satz, den Sektion 2 oder 2b findet, gehört nicht dazu – dieselben Abfragen, dasselbe
  Aufsummieren, keine neue Lesart; der Standard ohne den Schalter ist unverändert), dem Bericht der BWA (`build_report`)
  über die vollen Zeilen und den Töpfen des Dashboards (`_dashboard_movements`: Einnahme, privat, Ausgabe, Übertrag – je Spanne EIN Aufruf,
  nie ein Lauf über das Jahr, der nach Monat aufgeteilt wird; so ist Einnahme + privat + Ausgabe + Übertrag auf den Minor-Wert gleich der Karte `get_dashboard(…)['balance']` derselben Spanne). Die einzige neue Abfrage
  ist `_liquidity_unbooked` (Bankbewegungen ohne Buchungssatz, die kein Übertrag deckt – dieselbe Auswahl wie der Zweig „ohne Kind“ des Dashboards, Beträge über `_minor_or_zero`, die Übertragsrechnung
  `_settle_stock` nur, wenn eine solche Bewegung einen ganzzahligen Betrag ≠ 0 hat, und höchstens einmal je Aufruf). Vorhandene Funktionen wurden nicht umgebaut (`get_euer_data` bekam nur den Schalter, sein Standard bleibt
  Zeichen für Zeichen gleich); die Schnappschüsse (BWA-PDF, EÜR-Kasten, Export der
  Summen- und Saldenliste) bleiben unverändert. **Zeilen** (Minor Units, jeder Wert mit seiner Wirkung auf die flüssigen Mittel, Zufluss +; `R` = Saldo der EÜR-Zeile je Konto, `U` = Saldo des Kontos in den Umbuchungen
  zwischen Anlage- und Erfolgskonto, `get_euer_data(…, only_asset_transfers=True)`): (1) Ergebnis = das der BWA; (2) Abschreibungen = −Σ U der Konten der BWA-Gruppe `depreciation` (6200–6299); (3) Anlagenabgänge zum
  Restbuchwert = −Σ U der Konten 4855–4858 und 6895–6898 (`DISPOSAL_RANGES`); (3a) Übrige Umbuchungen ohne Geldfluss = −Σ U jedes anderen Kontos (steht nur bei einem Wert). **Seit 2.6.4 rechnet die Überleitung nur zurück,
  was die EÜR als Umbuchung zwischen Anlage- und Erfolgskonto liest** (AfA, Restbuchwert beim Abgang): Eine EÜR-Zeile allein füllt die Zeilen 2, 3 und 3a nie, und eine direkt bezahlte Abschreibung („6260 an Bank“) bewegt
  Geld, bleibt im Ergebnis und braucht keinen Rest. Die Prüfung geht erst nach `DISPOSAL_RANGES`, dann nach der Gruppe `depreciation`, sonst nach 3a (`_noncash_line`); im Kontennachweis steht der Betrag der Umbuchungen,
  nicht der ganze Saldo, den eine Zahlung auf demselben Konto mitträgt. (4) Darlehen und Verbindlichkeiten im Ergebnis = BWA-Gruppe `liabilities` (3000–3799 außer 3160, 3720, 3740),
  herausgerechnet; (5) Zwischensumme „Ergebnis vor Abschreibungen und Darlehen“ (1 + 2 + 3 + 3a + 4); (6) Anlagenzugänge = Σ R der Konten unter 1000 außerhalb des Ergebnisses; (7) Darlehen und Verbindlichkeiten = Σ R aller Konten 3000–3799
  (Aufnahme +, Tilgung −; die Zeilen 4 und 7 heben sich bis auf 3160, 3720 und 3740 auf); (8) Privatentnahmen 2100–2179; (9) Privateinlagen 2180–2199; (10) übrige Konten außerhalb des Ergebnisses (erwartet 0, steht nur bei einem Wert);
  (11) Veränderung laut Überleitung = 5 bis 10; (12) Bankbewegungen ohne Buchungssatz (die Bezeichnung nennt ihre Zahl im Monat und aufgelaufen); (13) **Nicht zugeordnet** = Ist − (11 + 12), steht immer da, auch bei 0,00;
  (14) Veränderung der flüssigen Mittel („Ist“) = Einnahme + privat + Ausgabe aller Bank- und Kassenkonten **ohne Überträge zwischen eigenen Konten** (Geld zwischen eigenen Konten ändert die flüssigen Mittel nicht); die Überträge
  und die Veränderung laut Kontobewegungen stehen nur nachrichtlich und nur, wenn Überträge dastehen. Die Bereiche stehen als Daten an EINER Stelle (`DISPOSAL_RANGES`, `FINANCING_RANGE`, `WITHDRAWAL_RANGE`, `DEPOSIT_RANGE`,
  `ASSET_LIMIT`) und gelten nur für ganzzahlige Kontonummern (eine Kommazahl aus der SQL-Konsole hat wie in `bwa.group_of` keinen Bereich); **jede EÜR-Zeile steht an genau einer Stelle** (ein Test je Grenze der Bereiche), zwei Zeilen gleicher Nummer werden je Zeitraum addiert und stehen als EINE Zeile unter dem zuerst gefundenen Namen.
  **Abstimmung:** „Nicht zugeordnet“ ist per Definition die Differenz und keine dritte Lesart – was die Überleitung aus den Buchungen nicht erklärt, bleibt dort sichtbar und wird in der Überleitung nie einer anderen Zeile zugeschlagen. **Grenze der Ist-Seite** (sie ist die des Dashboards): Trägt eine
  Bankbewegung einen Spiegel zwischen eigenen Konten, steckt der Teil ihres Betrags, den kein Satz erklärt, im Übertrag – ein unstimmiger Split neben einem Übertrag steht deshalb nur nachrichtlich unter den Überträgen,
  nicht in „Nicht zugeordnet“ (Test mit festen Werten; die Prüfung der Splits nennt die Bewegung). „Geht auf“
  heißt exakt 0 Minor Units; ein Rest unter einem halben Cent steht als 0,00 da und wird auf der Seite als Rundungsrest genannt, nie geglättet. Pflicht-Tests (`tests/test_liquidity.py`): Im Demobestand und in Beständen aus Zufall über
  Wege der App (`tests/liquidity_helpers.random_books`) ist sie in jedem Monat und aufgelaufen 0; in der SuSa-Welt (März +20,00, ab März aufgelaufen) und der Testwelt (April −70,00, Mai −55,00, Juni −1,00) stehen feste Werte als Maßstab –
  an ihnen darf sich nichts verschieben; für jeden benannten Fall ist festgehalten, was dort landet: ein Satz über das Verrechnungskonto 1460 in einem anderen Monat als seine Bankseite, ein Satz mit anderem Datum als seine Bankbewegung über eine
  Monatsgrenze, ein unstimmiger Split, ein Steuersatz außer 5, 7, 16 und 19 %, der Status `resolved`, eine Zahlung auf ein Bestandskonto ohne Gegenumbuchung,
  eine Umbuchung Anlage ↔ Erfolgskonto mit der Kasse als `Account_ID`, ein Spiegel alter Art mit 1460 und ein Rundungsrest. Es gehen auf (je ein Test, auch ihr Kontennachweis): die direkt bezahlte Sofortabschreibung
  „6260 an Bank“ (bleibt im Ergebnis; auch neben der AfA der App im selben Monat und auf demselben Konto 6220), die Umbuchung „6815 an 0520“ (Zeile 3a), ein Abgang über das Verrechnungskonto („6895 an 1460“ und „1460 an 0520“:
  Aufwand und Anlagenzugang heben sich auf) und die Umbuchung unter einer Bankbewegung (der Restbuchwert im Split des Verkaufs, „0670 an Bank“ mit „6260 an 0670“ in einem Beleg, eine im Editor zugeordnete AfA; trägt die Bewegung NUR
  die Umbuchung, bleibt ihr Betrag als unstimmiger Split in „Nicht zugeordnet“). Dazu prüft ein Test die Zerlegung von `get_euer_data(…, only_asset_transfers=True)`: Saldo(alle) − Saldo(nur Umbuchungen) = Saldo eines Bestands, aus dem genau diese Sätze gelöscht
  sind. **Diese Erwartungen nicht lockern** – ändert
  sich ein Wert, ist die Ursache zu klären. **Seite:** Untermenü der Auswertungen (`REPORT_PAGES`, vierter Eintrag „Liquidität“), `Header1('bwa')`; Zeitraum und Darstellung wie bei der BWA: Die Seite nimmt den gemeinsamen Zeitraum (`resolve_period`,
  das Cookie wie überall; ihr Monat folgt aus `period.period_month`) und liest aus der Adresse nur `variant` (`pages_bwa.read_variant`: unbekannt oder doppelt → Standard, ohne Hinweis); das PDF liest `year`, `month` und `variant`
  streng (`pages_bwa.read_pdf_query`: 400 bei einem Fehler) und setzt kein Cookie; nichts wird in den Bestand geschrieben; die Seite nutzt nur
  vorhandene Klassen (`bwaTable` …), ihr Skript nur `var` und `function` mit dem Präfix `liq`; unter den Zeilen mit Konten lassen sich diese aufklappen. Fester Hinweis: „Aus den Buchungen dieser App gerechnet. Keine Steuerberatung; ersetzt
  nicht die Auswertung eines Steuerbüros.“ – ohne Marken- und Produktnamen. **PDF:** A4 quer, mehrseitig, nie auf der Platte (Download über `appDownload`; `Liquiditaet_JJJJ-MM.pdf`, in der Darstellung „Monat und Jahr“ `…_Monat_Jahr.pdf`), auf Wunsch mit
  Kontennachweis, hinter der Tabelle ein Textblock mit dem Satz zur Abstimmung; seine Bausteine stehen in `export/pdf_table.py`. `/liquiditaet` steht in der Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/liquiditaet/pdf` nicht;
  die Routen stehen in keiner der Mengen `_ACTION_PATHS`, `_JSON_PATHS` und `_LOCK_FREE_ROUTES`. **Laufzeit** (gemessen an einem Bestand mit 200 000 Buchungssätzen): rund 2,5 s im Vorjahresvergleich (vier Spannen), rund 1,4 s als „Monat und Jahr“,
  im Demobestand `full` rund 10 bis 20 ms; je Spanne zwei Aufrufe von `get_euer_data` (alle Zeilen und nur die Umbuchungen, je rund 0,1 s); den größten Teil macht die Übertragsrechnung (`_settle_stock`, liest den ganzen Bestand,
  je Aufruf rund 0,3 s) aus – `_dashboard_movements` ruft sie je Spanne, die Liquiditätsrechnung selbst nur einmal je Aufruf.
  `tests/test_liquidity.py`, `tests/test_liquidity_pdf.py`, Hilfen `tests/liquidity_helpers.py`
- **Automatischer Abgleich** (`link_bank_to_entries`, läuft nach jedem Import; seit 2.3.14 mit Richtungsregel): Angefasst
  werden nur Bankbewegungen ohne Kinder und Sätze ohne `ParentBooking_ID` – eine bestehende Verknüpfung und der Text der
  Bankbewegung ändern sich nie. In den Stufen 1–6 (Reihenfolge im Lauf: 0, 1, 2, 4, 3, 3b, 3c, 3d, 5, 6, 6b, Nachlauf, 7) wird
  ein Satz nur verknüpft, wenn seine **Wirkung auf das Konto der Bankbewegung mit Vorzeichen** dem Betrag entspricht
  (`matching.link_effect` ∈ {`None`, `bank_effect`}; Toleranz unter 50 Minor Units = ein halber Cent), bei Gruppen die
  Summe der Wirkungen – jede automatische Verknüpfung ergibt damit eine ausgeglichene Split-Summe. `None` (nie Kandidat):
  `mirror_legacy` (Lesart unbekannt, wird nie gedeutet), ein Spiegel, der das Konto der Bewegung nicht berührt, jeder Satz
  mit einem ANDEREN Zahlungskonto auf einer Seite (Kasse, zweites eigenes Bankkonto, 1460), jeder Satz mit dem
  `Account_ID` eines anderen Kontos, ein Satz, dessen Betrag nicht als Zahl gespeichert ist – und **jeder Satz, bei dem das
  Konto der Bewegung nicht auf der Geldseite seiner Form steht** (`_MONEY_SIDES`): eine Umbuchung (`transfer`), die das
  Konto berührt oder nicht ganz kontiert ist, `purpose_first` mit dem Konto vorn, `liquid_first` mit dem Konto hinten. Dort
  wäre die Wirkung das Gegenteil des gespeicherten Betrags, `bank_effect` zählt ihn aber wie gespeichert – der Satz hinge
  an der Bewegung in der falschen Richtung, und die Split-Summe meldete nichts. Umbuchungen ohne Geldseite (Wirkung 0: „Wartekonto an Erlöskonto“, Privatanteil, AfA)
  zählen bei der Eindeutigkeit der Einzelstufen mit, werden aber nie allein verknüpft – nur in einer Gruppe mit dem Satz,
  der das Geld bewegt. Die Kandidaten sind `_Cand`-Tupel, Zugriff nur über Namen (`_do_link(bank_id, cand)` – so landet nie
  wieder ein falsches Feld im Banktext). Stufe 0, 6b, der Nachlauf und Stufe 7 rechneten schon mit Vorzeichen und sind
  unverändert. In Stufe 1 nimmt der Rückgriff „erster passender Satz des Empfängers“ nur, wenn keine Kennung trifft; zeigt
  die Kennung auf eine Umbuchung, bleibt ihr Beleg der Gruppenstufe überlassen. Bewusst nicht mehr automatisch verknüpft
  (die Richtung wäre nur vermutet): eine Umbuchung, deren Konto erst später Bankkonto wurde oder deren Gegenkonto
  leer/unbekannt ist (WISO-Textimport mit unbekanntem Konto), Sätze gegen 1460 ohne Quellklammer, Sätze gegen eine
  Kontonummer, die kein eigenes Konto ist (Bankkonto ohne SKR-Nummer). Sie bleiben frei – die Bankbewegung zeigt sich
  als „ohne Buchungssatz“, der Satz fehlt in der EÜR, bis er im Editor von Hand zugeordnet ist. Diese Regel nicht
  aufweichen – lieber eine Verknüpfung weniger als eine falsche (`tests/test_matching_direction.py`)
- Kasse = SKR04-Konto 1600 als eigenes Konto: Eine neu angelegte Kasse
  (`ensure_kasse_exists`, `insert_account`) bekommt 1600, das Konto wird bei Bedarf
  nachgelegt (`ensure_cash_coa`). Vorhandene Kassen behalten ihre Nummer (leer oder 1460);
  1460 ist das Verrechnungskonto, keine Kasse. Hat die Kasse keine SKR-Nummer, ist 1600
  kein Zahlungskonto
- AfA (`book_depreciation`): Umbuchung „Aufwand an Anlagekonto“ (direkte Abschreibung, Form
  `transfer`), Betrag positiv, ohne `Account_ID`, je Anlage und Jahr nur einmal (Sperre in einer
  Transaktion). Aufwandskonto = Erfolgskonto (4000–7999), Anlagekonto = Konto 1–999, nach
  dem Abgang der Anlage keine AfA mehr. „Gebucht“ heißt: die Buchung gibt es (oder
  WISO-`booked`). Löschen der Buchung setzt das Jahr auf `planned` zurück, `delete_asset`
  lehnt bei gebuchter AfA ab. In der EÜR (Sektion 3, ohne Steuer), nicht im Dashboard – die
  AfA ist kein Geldfluss. **Plan (`Database.depreciation_plan`, seit 2.2.6):** Die gebuchten Jahre sind Geschichte, der Rest
  verteilt sich neu. Gebucht ist, was in den Büchern steht – bei einem von der App gebuchten Jahr die Wirkung der verknüpften
  Buchung auf das Anlagekonto (`matching.depreciation_effect`: Anlagekonto im Haben +, im Soll −, nie unter 0), bei einem
  importierten Jahr (`booked` ohne Buchung) `DepreciationAmount`. **Unverändert-Regel:** Stimmt jedes gebuchte Jahr auf den
  Cent mit der Formel (`calculate_depreciation_plan`, unverändert: linear, degressiv, GWG) überein, ist der Plan genau die
  Formel – für eine nur in der App geführte Anlage ändert sich nichts. **Fortschreibung** bei abweichender Geschichte
  (Import, geänderter Anschaffungswert oder geänderte AfA-Buchung): Restbuchwert = Anschaffungswert − gebuchte Beträge, ab
  dem Jahr nach dem größten gebuchten Jahr linear nach Monaten bis zum Planende (Kaufmonat + 12 × Nutzungsdauer), ganzzahlig
  in Cent, das letzte Jahr den Rest; abgelaufene Nutzungsdauer → ganzer Rest im nächsten Jahr; Jahre mit 0 Cent entfallen;
  keine GWG-/degressive Regel; Buchwerte fortlaufend aus dem heutigen Anschaffungswert. In der Fortschreibung ist **nur das
  erste offene Jahr buchbar** (`bookable`; Lücken bekommen keine Zeile, ihr Betrag steckt im Rest). `book_depreciation` liest
  den Plan in seiner Transaktion (`BEGIN IMMEDIATE`), bucht genau den Planbetrag; Anlagenseiten, `get_book_value_at_date`
  und die Prüfungen des WISO-Imports nehmen denselben Plan. **Der Plan endet mit dem Abgangsjahr** (`SaleDate`, die ersten
  vier Zeichen): offene Jahre danach entfallen – in der Unverändert-Regel wie in der Fortschreibung –, gebuchte bleiben;
  `book_depreciation` lehnt ein Jahr danach noch vor der Planprüfung mit dem Abgang als Begründung ab, und
  `get_book_value_at_date` ist 0, sobald der Abgang erreicht ist: bei einem ISO-`SaleDate` am Abgangstag und danach, bei
  einem Datum, das kein ISO-Datum ist, aber mit einem Jahr beginnt, ab dem Jahr danach (wie das Planende); ohne lesbares
  Jahr gilt kein Abgang (die zeitanteilige AfA im Abgangsjahr bleibt offen).
  **Erweiterungen (seit 2.2.7):** Nachträgliche Anschaffungskosten (Unter-Anlagen, `Assets.Parent_ID`) erhöhen den
  Anschaffungswert der Hauptanlage. Eine Unter-Anlage **zählt** (`Database.counts_toward_parent`), wenn ihre Hauptanlage
  einen Plan haben kann (lesbares Kaufdatum, Preis > 0 Cent, Nutzungsdauer > 0), sie **gar keine eigene Zeile in
  `AssetDepreciations`** (gleich welchen Status) und keine eigenen Unter-Anlagen hat und ihr Datum und ihr Preis (> 0)
  lesbar sind. „Keine Zeile“ statt „kein gebuchtes Jahr“, weil die Planzeile nach dem Löschen der einzigen AfA-Buchung als
  `planned` stehen bleibt: Die Unter-Anlage bleibt eine eigene Anlage und lässt sich neu buchen, statt ihren Betrag in die
  Zukunft der Hauptanlage zu verschieben. Sie hat **keinen eigenen Plan** (`depreciation_plan` leer, `get_book_value_at_date`
  0), wird nicht gebucht (`book_depreciation` lehnt ab: Die AfA läuft über die Hauptanlage), hat keinen eigenen Abgang
  (`sell_asset` lehnt ab, die Seite zeigt das Formular nicht) und keinen eigenen Status (`handle_update_asset` lehnt einen
  Status ≠ `active` ab: „Eine Erweiterung behält den Status der Hauptanlage.“); Nutzungsdauer, Methode und Anlagekonto der
  Hauptanlage gelten (kein eigener Codepfad: gebucht wird allein an der Hauptanlage). Eine Unter-Anlage mit eigener AfA-Zeile
  (Bestand, WISO) oder unter einer Hauptanlage ohne Plan bleibt eine eigene Anlage mit ihrem Plan (wie bis 2.2.6) und fließt
  nicht in die Hauptanlage. **Löschen** (`delete_asset`): Eine zählende Erweiterung lässt sich nicht löschen, sobald die
  Hauptanlage seit dem Jahr der Erweiterung (nie vor ihrem Kaufjahr) ein gebuchtes Jahr (`_DEPRECIATION_BOOKED`) hat – sie
  steckt in diesen Buchungen; ohne ein solches Jahr schon. Maßgeblich ist das Anschaffungsdatum der Erweiterung (wann die Kosten
  entstehen, nicht die Zahlung), ihr Jahr nie vor dem Kaufjahr der Hauptanlage. Im Jahr der Erweiterung zählt sie **wie zu
  Jahresbeginn aufgewendet** (keine Kürzung nach Monaten): Restbuchwert plus Erweiterung verteilen sich linear nach Monaten
  auf die Restnutzungsdauer (`_continue`). **Ausnahme Anschaffungsjahr:** Die Formel rechnet auf Anschaffungswert plus
  Erweiterung, ab dem Kaufmonat der Hauptanlage. Ab der ersten späteren Erweiterung rechnet der erwartete Plan linear über die
  Restnutzungsdauer (auch bei degressiver Methode oder GWG der Hauptanlage – dieselbe Vereinfachung wie die Fortschreibung),
  und die Unverändert-Regel vergleicht die gebuchten Jahre mit diesem Plan. Entsteht eine Erweiterung in einem schon
  gebuchten Jahr, stimmt die Geschichte nicht mehr: Fortschreibung, ihr Betrag geht in den Rest ab dem ersten offenen Jahr
  (die Buchwerte der gebuchten Jahre rechnen vom Anschaffungswert samt Erweiterungen bis zu diesem Jahr). Nach dem Ende der
  Nutzungsdauer steht der ganze Betrag im Jahr der Erweiterung. `Database.acquisition_cost(asset_id, year)` = Preis plus
  zählende Erweiterungen mit Jahr ≤ `year` (Standard: laufendes Jahr; die Anlagenliste zeigt ihn). `handle_add_asset` legt
  keine Erweiterung einer Erweiterung an
- Rechnungszahlung: `assign_invoice_payment` (`db/invoice_payments.py`) ist die einzige Stelle, die
  eine Zahlung Rechnung ↔ Bankbewegung zuordnet, `assign_storno_refund` die einzige, die eine Stornorechnung
  erstattet (*Stornobeleg*), `unassign_invoice_payment` die einzige, die eine Zuordnung
  entfernt (`delete_transaction` geht darüber, wenn die Bankbewegung oder ein markierter Satz fällt). Eine
  Zuordnung = eine `InvoicePayments`-Zeile + „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ (ohne
  Forderung: „Erlöskonto an Bank“), erkennbar an `ParentBooking_ID` = Bankbewegung, `DocumentNumber` =
  Rechnungsnummer und `Status='invoice_payment'` (Formen: „Bank an Debitoren“ `liquid_first`,
  „Wartekonto an Erlöskonto“ `transfer`, „Erlöskonto an Bank“ `purpose_first`; die Forderung ist
  `transfer`; ein eindeutiger Index auf `InvoicePayments(InvoiceID, BookingID)` verhindert das Paar
  doppelt – fehlt er, weil ein Bestand Dubletten trägt, startet die App trotzdem und die Prüfliste
  meldet sie). Entfernt werden nur diese markierten Sätze, nie Sätze
  mit `SourceGroup` und nie unmarkierte (Altbestand, WISO). Prüfung, Schreiben und Neuberechnung laufen
  in einer `BEGIN IMMEDIATE`-Transaktion (der Index fängt zusätzlich ab: `IntegrityError` heißt „bereits verknüpft“); Konten, die `resolve_revenue_coa` anlegen
  könnte, werden vorher in eigenen Verbindungen aufgelöst – nie innerhalb. Die inneren Bausteine, auf denen
  beide Wege und der Umzug beim Versenden einer Ersatzrechnung aufsetzen: `_assign_in_tx(tx, …)` (der
  Transaktionsteil des Zuordnens, committet nie; `allow_cancelled` öffnet eine stornierte Rechnung mit positivem
  Betrag – nie eine Stornorechnung – und gilt allein für den Rest, der beim Umzug an der stornierten Rechnung bleibt)
  und `_unassign(conn, …)`. Nur Zahlungseingänge
  (Betrag > 0) auf versendete Rechnungen (`PAYABLE_STATUSES`), Bankkonto mit SKR-Nummer; frei ist
  `Betrag − MAX(Zuordnungen, bankwirksame Sätze)` (`_free_minor_sql`, überall dieselbe Formel – sie
  rechnet Spiegel roh, die Split-Summe dagegen über `bank_effect`, der einen Spiegel mit bekannter
  Lesart nach seiner Position zählt; beides ist bewusst nicht dasselbe). **Der freie Rest eines Ausgangs (Erstattung)
  rechnet über `bank_effect`** (`_outgoing_free_minor`, in Python): `|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze auf das
  Konto der Bewegung)` – wie die Split-Summe, anders als `_free_minor_sql` für Eingänge, das einen Spiegel roh rechnet
  (bewusst): Der Import-Spiegel „1810 an 1800“ mit +50 unter einem Ausgang über 50 (`mirror_coa`) verbraucht ihn, die
  Umbuchung ist keine Erstattung. Zuordnungen je Bewegung, Sätze je Bewegung und das Konto der Bewegung kommen aus wenigen
  gesammelten Abfragen (die Kandidatenliste `get_unallocated_outgoing_bookings` wächst linear). Der
  Zuordnungsbetrag wird auf Cent gerundet (halb auf), bevor er gekappt wird; die Steuer eines Teils ist
  anteilig (Cent), der schließende Teil bekommt den Rest der Rechnungssteuer abzüglich der Steuer der
  anderen Zuordnungen (markierte Sätze: die gebuchte, sonst – Altbestand, WISO – die anteilige).
  **Erlöskonto:** Es ergibt sich aus der Rechnung (`resolve_revenue_coa`) und wird NUR aus den markierten
  Sätzen mit positivem Betrag gelernt (die einer Erstattung lehren nichts), und nur in der Form, in der die App sie
  schreibt (Umbuchung „Wartekonto an Erlöskonto“: `COA` ist ein Wartekonto, gelernt wird das Gegenkonto; Einzelsatz „Erlöskonto an Bank“: das Gegenkonto
  ist ein Zahlungskonto, gelernt wird `COA`; der jüngste gewinnt) – was ein Nutzer im Split-Editor in
  eine andere Form gebracht hat, lehrt nichts. Es gilt nur als Erlöskonto 4000–4999 ohne Wartekonten,
  sonst 4400/4300/4185. Das Konto der Bankzeile (Anzeige-Backfill) und Sätze des WISO-Imports lehren
  nichts – es kann ein Bank-, Aufwands- oder Wartekonto sein. Ist für den Satz (z. B. 5 %, 16 %)
  keines hinterlegt, wählt der Nutzer es (`revenue_coa_id`): ein Erlöskonto, in der DB-Schicht
  geprüft, sonst Ablehnung; `suggest_revenue_coa` ist nur die Vorauswahl, nie eine Entscheidung.
  Status und Rest berechnet `next_payment_status` (nie zurück auf „abgeschlossen“, „storniert“
  bleibt; „Rest unter einem Cent gilt als beglichen“ steht in `money.is_covered`).
  **Die Forderung (`10000 an Wartekonto`) ist nie Kind einer Bankbewegung:** `matching.receivable_condition`
  (10000 im SOLL – `COA` = 10000 bei Betrag > 0 oder Gegenkonto = 10000 bei Betrag < 0, bei einer
  Gutschrift `COA` = 10000 mit negativem Betrag und der Beleg-Nr. einer Rechnung mit negativem Brutto –
  und keine Seite ein Zahlungskonto) schließt sie aus Auto-Abgleich und Editor-Kandidaten aus,
  `delete_transaction` hängt sie ab statt sie zu löschen. Was 10000 im Haben führt (Erlösminderung,
  Skonto, Forderungsverlust „… an 10000“), ist keine Forderung und geht mit seiner Bewegung. Alle
  Stellen suchen die Forderung über diese Bedingung (auch `assign_invoice_payment`,
  `get_receivable_booking`). In der Buchungsliste trägt die Forderung (eigenständige Zeile) seit 2.6.6 das graue Abzeichen „Forderung“
  statt des Hakens „Buchung vollständig gebucht“ und einen ungefärbten Betrag – sie ist kein Geldeingang und noch keine Einnahme
  (`Database.receivable_entry_ids`, EINE Abfrage je Seite; Gruppen- und Kindzeilen bleiben, wie sie waren,
  `tests/test_receivable_row.py`). Stufe 7 des
  Auto-Abgleichs setzt sie erst auf `resolved`, wenn die Summe der Sätze „… an 10000“ mit gleicher
  Beleg-Nr. den Forderungsbetrag erreicht; `unassign` setzt das zurück. Die Forderung geht mit der
  letzten Zahlung, wenn der Status der Rechnung kein Forderungsstatus ist (`RECEIVABLE_STATUSES`, z. B.
  eine von Hand stornierte Rechnung: Altbestand oder ohne positiven Betrag) – gleicher Endzustand, egal ob erst storniert oder erst
  entfernt wird; **entfernt wird nur die Forderung, die die App selbst gebucht hat** (`_receivable_removal`:
  ohne `SourceGroup`, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet), nie eine importierte oder von
  Hand gebuchte. **Ausnahme Storno-Paar:** Hat die Rechnung eine Stornorechnung (oder ist sie eine), bleiben die
  Forderung der Ursprungsrechnung und die Gegenbuchung der Stornorechnung in jedem Fall stehen
  (`_drop_receivable`, auch für `remove_receivable_booking`) – was mit den Zahlungen geschieht, ändert daran nichts.
  Negative Zuordnungen kennen der WISO-Import (`link_invoice_to_transaction`, mit Vorzeichen, ohne
  Sätze) und die Erstattung einer Stornorechnung (`assign_storno_refund`, mit Sätzen).
  `find_payment_inconsistencies` prüft nur lesend und linear im Bestand (Seite *Sonstiges*:
  nur auf Klick, `?check_payments=1`, nie beim Laden; Hinweis auf der Rechnungsseite); Zuordnungen auf
  Nicht-Bankbewegungen oder mit `SourceGroup`-Sätzen (WISO) löst `no_settlement`/`transfer_mismatch`
  nicht aus. Zwei Kategorien gelten dem Storno-Paar und zählen nur Forderungen, die die App selbst gebucht hat
  (`_app_receivable_ids`; ein Import löst keinen Fehlalarm aus): `storno_counter_mismatch` (von Forderung der
  Rechnung und Gegenbuchung der Stornorechnung steht nur eine) und `legacy_cancelled_open` (von Hand storniert,
  keine Stornorechnung, die Forderung steht noch); ein intaktes Paar, auch mit Guthaben oder Erstattung, ergibt keinen Befund.
  Dazu `refund_exceeds_credit` (Σ Zuordnungen beider Belege < 0: mehr erstattet als gezahlt – kein Weg der
  Oberfläche lässt es entstehen; die Kategorie meldet, was daran vorbei geschieht: SQL-Konsole, Sicherung, Bestand älterer
  Versionen und ein Import, der nach einer Erstattung eine negative Zuordnung an die stornierte Rechnung hängt
  (`link_invoice_to_transaction` prüft das nicht); je Rechnung vorab zusammengefasst).
  **Diese Forderungen liest die Prüfliste einmal für alle Paare und Alt-Stornos** (`_app_receivable_amounts`, mit
  Beträgen; dieselbe Bedingung `receivable_condition` und dieselbe Einordnung `_is_app_receivable` wie
  `_app_receivable_ids`, das darauf aufsetzt) – je Rechnung nachzuschlagen wäre quadratisch, `DocumentNumber` hat keinen
  Index; nur der Filter auf eine Rechnung (`invoice_id`) liest gezielt. Die Reparatur je Rechnung ist *Zuordnung neu
  aufbauen* (nächster Punkt)
- **Zuordnung neu aufbauen (seit 2.5.13, `db/invoice_repair.py`, `Database.rebuild_invoice_payments`; `POST /invoice/rebuild-payments`,
  JSON `{invoiceId[, execute, check, revenueCoaId]}`):** Die Reparatur der Zahlungszuordnung **einer** Rechnung (es gibt keine
  Sammelreparatur), nur nach einem Probelauf und mit Bestätigung. Sie macht vier Dinge, in dieser Reihenfolge: (A) doppelte Zuordnungen
  desselben Paares (Rechnung, Bewegung) mit gleichem Betrag zusammenfassen – die kleinste ID bleibt, verschiedene Beträge nur melden –,
  (B) die als Kind an einer Bankbewegung hängende Forderung der App (genau eine, `_is_app_receivable`) lösen (`ParentBooking_ID` leer, ein
  Status `resolved` zurück – wie `remove_split_row`), (C) jede Zuordnung ohne eigene Sätze mit den heutigen Bausteinen neu schreiben
  (`_unassign` + `_assign_in_tx`, **in zwei Phasen in EINEM Savepoint:** erst alle Kandidatinnen entfernen, dann in der Reihenfolge der
  Bankbewegungen (ihr Datum, dann die ID der Zuordnung) neu buchen – so verteilt sich die Steuer wie beim Zuordnen nacheinander: die späteren
  zählen noch nicht als bezahlt, die letzte, die die Rechnung schließt, bekommt den Rest, und nicht angefasste Zuordnungen zählen als schon
  bezahlt (das bisherige Zahlungsdatum einer Zuordnung bestimmt die Reihenfolge nicht). Geht eine
  nicht mit genau ihrem Betrag, nimmt der Savepoint den ganzen Durchgang zurück (Zuordnungen, Sätze, Rechnung und die Einträge fürs Replay-Log),
  sie bleibt samt ID liegen und wird als `not_free` gemeldet, und die übrigen laufen ohne sie noch einmal – höchstens so viele Durchgänge, wie
  es Kandidatinnen gibt. Eine Notiz der alten Zuordnung bleibt; das Zahlungsdatum der neuen ist das Datum der Bankbewegung, und weicht das
  bisherige ab, nennt es der Text der Aktion – mit dem alten Wert nur, wenn der ein ISO-Datum ist), (D) den offenen Betrag neu rechnen – **der Status bleibt** (eine von Hand auf
  „bezahlt“ gesetzte Rechnung bleibt es), nur nach A rechnet `next_payment_status` ihn mit dem Tag `today` neu. **Es wird kein Buchungssatz
  gelöscht oder geändert** (B ist die eine Ausnahme: nur `ParentBooking_ID`/`Status` der Forderung); neue Sätze kommen nur dazu, mit Datum und
  Steuerdatum der Bankbewegung und der Marke. `_booking_snapshot_unchanged` prüft es mechanisch: Die vorhandenen Sätze der Rechnung, ihrer
  Bewegungen und deren Kinder müssen nach dem Schreiben Spalte für Spalte gleich sein (die gelöste Forderung ausgenommen) – sonst nimmt die
  Reparatur alles zurück und meldet es (409, `REBUILD_UNSAFE_MESSAGE`). **Ein zweites Netz** liest die Prüfliste nach dem Schreiben noch einmal:
  Nennt sie eine Kategorie, die sie vorher nicht nannte (etwa `transfer_mismatch`, wenn ein Erlös doppelt gebucht würde), geht ebenso alles
  zurück (409, `REBUILD_UNSAFE_MESSAGE`); die Kategorien stehen nur im Server-Log, nie in der Antwort. Storno-Paare (die Rechnung hat eine Stornorechnung oder ist eine),
  Rechnungen außerhalb von versendet/überfällig/Teilzahlung/bezahlt und ohne positiven Betrag bekommen nur den neu gerechneten offenen Betrag.
  **Bedingungen für C („R1“), alle zugleich:** die Zuordnung hängt an einer Bankbewegung mit Zahlungseingang; unter der Bewegung steht kein Satz
  mit `SourceGroup` (Import: still übergangen) und keiner mit der Nummer dieser Rechnung (außer der in B gelösten Forderung); jeder andere
  Satz trägt die Nummer einer anderen Rechnung, die selbst der Bewegung zugeordnet ist; die Forderung der Rechnung ist genau eine und von der
  App; das Bankkonto hat eine SKR-Nummer; der Betrag hat keinen Bruchteil eines Cents; für den Steuersatz ist ein Erlöskonto hinterlegt oder
  gewählt (`resolve_revenue_coa(…, create=False)` – **die Reparatur legt nie ein Konto an**, auch der Probelauf nicht; fehlt es, kommt
  `needs_revenue_coa` vor dem ersten Schreiben). **Dazu gilt für die ganze Rechnung „nichts an anderer Stelle“** (`_rows_elsewhere`, gelesen vor
  jeder Kandidatin; Grund `elsewhere`, einmal je Rechnung; A, B und D laufen weiter): Es gibt keinen Buchungssatz, der keine Bankzeile ist, die
  Nummer der Rechnung trägt (ohne Leerraum am Rand – alles, was `str.strip()` entfernt, auch Tabulator, Zeilenumbruch und geschütztes
  Leerzeichen: `_EDGE_WHITESPACE` – und ohne Rücksicht auf die Schreibweise: `LOWER(TRIM(…, _EDGE_WHITESPACE))`), nicht ihre Forderung ist und nicht
  unter einer Bankbewegung steht, der diese Rechnung zugeordnet ist – also keinen eigenständigen Satz (auch nicht auf der Kasse), keinen unter
  einer fremden Bewegung und keinen mit `SourceGroup` –, und die Prüfliste nennt für die Rechnung keinen Befund `orphan_payment_rows`; sonst
  stünde der Erlös nach dem Neuaufbau zweimal in den Büchern. Der Grund steht nur im Plan, wenn dadurch eine Zuordnung nicht aufgebaut wird.
  **Und die vorhandenen Sätze buchen nicht mehr Erlös, als die übrigen Zuordnungen erklären** (Erlös = Betrag der Sätze, die auf einer Seite ein
  Erlöskonto führen, ohne Wartekonten: „Wartekonto an Erlöskonto“ und „Erlöskonto an Bank“ je einmal, „Bank an Debitoren“ nie; ein Betrag, der keine
  ganze Zahl ist, sperrt): **je Rechnung** (`_overbooked`, Grund `overbooked`, einmal je Rechnung; A, B und D laufen weiter) liegt der Erlös aller Sätze
  mit ihrer Nummer (außer der Forderung) nicht über der Summe ihrer Zuordnungen, die bleiben – nicht zusammengefasst, nicht neu gebaut –, **je Bewegung**
  (`_overbooked_movement`, Grund `overbooked_movement`) der Erlös der Sätze unter ihr (sie gehören alle anderen zugeordneten Rechnungen) nicht über der
  Summe von deren Zuordnungen dort; sonst stünde der Erlös nach dem Neuaufbau zweimal in den Büchern.
  **Diese Bedingungen nicht lockern – lieber melden als falsch reparieren.** **Grenze:** Einen Satz ohne die Rechnungsnummer, der denselben Erlös
  schon bucht, erkennt die Funktion nicht – deshalb zeigt der Probelauf die Wirkung auf die EÜR, und der Hinweis im Plan bittet um die Prüfung.
  Was nicht angefasst wird, steht mit festem Grund in `skipped` (`REBUILD_SKIP_REASONS`). **Probelauf und Ausführung sind derselbe Code** (`_rebuild_in`,
  EINE `BEGIN IMMEDIATE`-Transaktion): Der Probelauf endet mit ROLLBACK und hinterlässt nichts – keine Zeile, keinen Zähler (`sqlite_sequence`),
  keinen Eintrag im Replay-Log (der folgt wie sonst erst dem Commit); die Ausführung schreibt nur mit `confirmed_check` = `RebuildPlan.check`,
  der SHA-256-Prüfsumme über Aktionen, neue Sätze (alle Spalten, nie ihre IDs), Wirkung auf die EÜR, Status, offenen Betrag, Befunde vorher
  und nachher, Gründe, Erlöskonto und – nur wenn Dubletten zusammengefasst werden, denn nur dann hängt der Status vom Tag ab – den Tag; nie an
  `UpdatedAt`. Hat sich der Bestand seither geändert (oder fehlt die Bestätigung), kommt
  409 mit dem frischen Plan, und nichts ist geschrieben. Der Plan nennt die Wirkung auf die EÜR je Datum und Erlöskonto (netto, Umsatzsteuer)
  und fest den Hinweis, dass ein schon gemeldeter Zeitraum dort nachzutragen ist, und die Bitte, vorher zu prüfen, dass Zahlung und Erlös nicht
  schon an anderer Stelle gebucht sind (keine Steuerberatung); die Seite zeigt ihn nur, wenn der Plan neue Buchungssätze enthält (der Knopf
  „Neu aufbauen“ steht bei jeder Aktion). Die Route (`_JSON_PATHS` +
  `_POST_ONLY_JSON_PATHS`, GET → 405) liest die ID über `_json_positive_id`, `execute` als Wahrheitswert, `check` als 64 Hexziffern (nur mit
  `execute`) und `revenueCoaId` streng; ein unerwarteter Fehler ist 500 mit `REBUILD_FAILED`, nie ein Ausnahmetext. Die Seite der Rechnung
  zeigt Knopf und Plan unter der Befundliste (nur mit Befund, nicht an einer Stornorechnung; das Skript setzt Werte nur über `textContent`);
  *Sonstiges* verweist darauf. `tests/test_invoice_payment_repair.py`
- **Besteuerungsart (seit 2.4.0):** Kleine Variante – die Angabe wird nur gespeichert und angezeigt. `Contacts.VatMethod` (Schema 2004)
  nennt, ob die Umsatzsteuer nach vereinnahmten (`'ist'`, Ist-Versteuerung: mit der Zahlung) oder vereinbarten Entgelten (`'soll'`,
  Soll-Versteuerung: mit der Leistung) entsteht; NULL = nicht festgelegt. Es trägt sie nur ein eigener Kontakt (`ContactType='own'`, Firma
  ODER Person – deshalb an `Contacts`, nicht an `CompanyDetails`); jeder andere bekommt NULL, und ein eigener Kontakt, der den Typ wechselt,
  verliert sie. **Die Werte stehen an EINER Stelle** (`db/contacts.py`: `VAT_METHODS`, `VAT_METHOD_LABELS`, `VAT_METHOD_ERROR`) und werden
  **streng gelesen** (`vat_method()`: genau `'ist'` oder `'soll'` als Text; alles andere – per SQL-Konsole `'IST'`, `' ist'`, `1` – gilt als
  nicht festgelegt und erscheint nie roh). `Database.get_vat_method()` liefert die Angabe des ersten eigenen Kontakts (nach ID) mit
  gültigem Wert, rein lesend. Geschrieben wird über `insert_contact`/`update_contact(vat_method=…)`: `None` lässt den gespeicherten Wert,
  `''` leert, `'ist'`/`'soll'` setzen, alles andere ist `ValueError(VAT_METHOD_ERROR)`; die Handler lesen das Formularfeld `vat_method`
  (fehlt es: unverändert; ein ungültiger Wert: 303 mit dem Klartext auf dem Fehlerweg der Kontaktseite, nichts gespeichert). **Sie ändert
  nichts an Buchungen, EÜR, Dashboard und Export:** Die App bucht Erlös und Umsatzsteuer weiter mit dem Zahlungseingang (Wartekonto →
  Erlöskonto, siehe *Rechnungszahlung*) – das Muster der Ist-Versteuerung, auch bei eingetragener Soll-Versteuerung
  (`tests/test_vat_method_setting.py` hält EÜR, Dashboard und DATEV-Stapel mit `ist`, `soll` und ohne Angabe gleich). Angezeigt wird sie
  im Formular eines eigenen Kontakts (Auswahl „Besteuerungsart (Umsatzsteuer)“ mit festem Hinweistext, `server/pages_contacts.py`; ein neu
  angelegter Kontakt bekommt das Feld wie die DATEV-Nummern erst, wenn er als „Eigen“ gespeichert ist) und – nur bei `soll` – als fester
  Hinweis unter dem DATEV-Formular auf *Sonstiges*. Keine Steuerberatung in den Texten. Die Umsetzung der Soll-Versteuerung
  (Umsatzsteuer mit der Leistung) ist offen (`docs/aufgaben.md`) – die Einstellung nicht für Rechenlogik verwenden, bevor das entschieden ist.
- Masken schreiben nur, was sie führen (`update_booking_fields`) – Gegenkonto,
  Kategorie und fremde Kontakte dürfen beim Speichern nicht verloren gehen. Der Block „Belege“ steht außerhalb des
  Formulars, das Feld Beleg-Nr. füllt das Skript nur, wenn es leer ist (*Belege verknüpfen*). **Öffnen und unverändert
  speichern ändert nichts** (`tests/test_entry_form_roundtrip.py` prüft es für jede Bankbewegung, jeden
  Satz und das Zuordnen an einem migrierten und einem frischen Bestand, nach geänderter Kontenliste):
  Welches Konto der Editor als Zweckkonto zeigt, sagt `matching.purpose_in_counter` – `liquid_first`:
  das Gegenkonto, `purpose_first`/`transfer`: das Konto, Spiegel und Zeilen ohne Form wie in 2.0.x
  („Bank an 1460“ zeigt 1460: Konto ist ein eigenes Bankkonto, Gegenkonto nicht). Editor, Maske und
  „vorhandene Buchung zuordnen“ nutzen dieselbe Regel; ein unverändert zurückgeschicktes Konto ändert
  nichts (verglichen mit dem gespeicherten Stand, nicht mit der Kontenliste von heute), ein unverändert
  zurückgeschickter Betrag gilt beim Wechsel des Zweckkontos als „nicht geändert“ (Soll/Haben bleibt: die
  Form kippt, der Betrag wird negiert). Ausgeblendet wird ein Spiegel nur, wenn beide Seiten eigene
  Bank-/Kassenkonten sind (`is_mirror_row`) – einer mit 1460 bleibt sichtbar und bearbeitbar, die
  Summenzeile des Editors rechnet ihn wie die Prüfung (`data-sign`, `bank_effect`). **Kein Schreibweg der
  Masken schreibt dasselbe Konto auf beide Seiten** (400 mit Klartext, `_refuse_same_account`); ein so
  vorgefundener Satz wird nicht angefasst. **„Vorhandene Buchung zuordnen“ nimmt die Buchung, wie sie
  ist: Steuersatz und -betrag bleiben, solange die Zeile sie nicht ändert** – die Option der Auswahl trägt
  sie (`data-rate`, `data-tax`), das Skript füllt damit die Zeile, und der Editor schreibt die Steuer
  eines bestehenden Satzes nur, wenn das Formular sie mitbringt (`_plan_split_rows`: fehlt das Feld, oder
  sind beim Zuordnen beide leer – so schickt sie ein Browser mit altem Skript –, bleibt der gespeicherte
  Wert; bei einem Satz unter der Bankbewegung löschen leere Felder die Steuer wie in jeder Zeile). Der
  Server rechnet die Steuer einer Zeile nie nach. **Das Zuordnen überschreibt die Geldseite nicht:** Steht
  hinten (Zweckkonto vorne) schon ein anderes Zahlungskonto – Kasse, andere eigene Bank, 1460
  (`get_payment_coa_ids`) –, behält der Satz Konten und Form und zählt in der Split-Summe, wie `bank_effect`
  ihn zählt; nur ein leeres Gegenkonto füllt das Konto der Bank (`tests/test_split_editor_money_side.py`,
  Eigenschaft „Zuordnen + ✕ stellt jeden gelösten Satz wieder her“ in `test_entry_form_roundtrip.py`).
  **Datum, Steuerdatum und Empfänger eines Satzes folgen der Bankbewegung nur, wenn er sie vorher mit ihr
  geteilt hat** (gleicher Wert wie die Bankbewegung bis
  jetzt, leer = leer; je Feld für sich): Ein Satz mit eigenem Wert – eine zugeordnete Bestandsbuchung, ein
  verknüpfter Beleg – behält ihn über jedes Speichern; neue Zeilen bekommen die Werte der Bankbewegung
  (`tests/test_split_editor_assignment.py`). **Entfernt der Nutzer die Zeile eines Satzes, der nachweislich
  nicht im Editor entstanden ist – importiert (`SourceGroup`), eine AfA-Buchung (ein Plan-Jahr verweist
  auf sie) oder eine als Kind angehängte Forderung (`receivable_condition`) –, wird er abgehängt statt gelöscht** (`Database.remove_split_row`: `ParentBooking_ID` leer, ein
  Status `resolved` zurückgesetzt, sonst jede Spalte unverändert; das AfA-Jahr bleibt gebucht, der nächste
  Abgleich findet ihn wieder); jede andere Zeile wird gelöscht, ein markierter Zahlungssatz mit seiner
  ganzen Zuordnung (nicht, wenn danach mehr erstattet als gezahlt wäre: Ablehnung vor dem ersten Schreiben, siehe
  *Stornobeleg*), und `delete_transaction` aus der Liste bleibt ein ausdrückliches Löschen
  (`tests/test_split_editor_removal.py`)
- **Speichern im Bank-Editor (seit 2.3.15) ist EINE Transaktion** (`Database.save_bank_with_rows(bank_id, bank_fields, plan)`,
  `db/bookings.py`): `BEGIN IMMEDIATE` → Stand der Bankzeile lesen → **planen** (`handlers._plan_split_rows`, liest über eigene
  Verbindungen – unter der Sperre ist der Stand stabil; deshalb VOR dem ersten Schreiben, und im Plan nie etwas schreiben) →
  Bankzeile → Sätze (`_write_split_rows_in`: neu, geändert, zugeordnet; entfernte Zeilen über `_remove_split_row_in` abhängen
  oder löschen) → Prüfung der zugeordneten Zahlung → Commit; das Replay-Log (`tx.log`) wird erst danach geschrieben (Bankzeile,
  Zeilen, Abhängen). Jeder Fehler und jede Ablehnung nimmt alles zurück. **Innerhalb der Transaktion nie eine öffentliche
  Methode aufrufen, die selbst schreibt oder `BEGIN` ausführt** (`delete_transaction`, `remove_split_row`,
  `unassign_invoice_payment`, `update_booking_fields` ohne `tx`) – sie hinge an der eigenen Sperre; dafür gibt es die Bausteine
  mit `tx`/`conn` (`update_booking_fields(tx=…)`, `insert_booking(tx=…)`, `_delete_transaction_in`, `_remove_split_row_in`,
  `_split_rows_dropped`). Außerhalb bleiben, wie bisher: `consume_number`, `link_booking_to_invoice_capped` und – beim Speichern
  einer Bankzeile ohne Editor – Spiegel und automatisches Kind. **Zuordnen lässt sich nur, was die Auswahl anbietet**
  (`Database.adoptable_entry_ids`: dieselbe Bedingung wie `get_unlinked_entry_bookings`, ohne `LIMIT` – freier `entry` ohne
  `Account_ID`, nicht `resolved`, kein markierter Zahlungssatz, keine Forderung) plus die eigenen Kinder der Bewegung: Jede andere ID in `split_id` – Kind
  einer anderen Bewegung, Bankzeile, die Bewegung selbst, Forderung, unbekannt, keine Ganzzahl, doppelt – ist 400 mit „Zeile n:
  …“, auch bei einer neuen Bewegung, und nichts ist gespeichert (`tests/test_split_editor_adopt_guard.py`,
  `test_split_editor_atomic.py`). `transaction_id` wird streng gelesen (leer/0 = neu, sonst positive Ganzzahl oder 400); der
  Editor nimmt höchstens `MAX_SPLIT_ROWS` = 500 Zeilen je Anfrage an (geplant und geschrieben wird unter der Schreibsperre)
- **Bankbewegung mit zugeordneter Zahlung (seit 2.3.15, Entscheidung des Owners: ablehnen, nichts umrechnen):** Solange einer
  Bankbewegung nach dem Speichern noch eine Zuordnung gehört (`InvoicePayments.BookingID` zeigt auf sie oder auf einen ihrer
  Buchungssätze – so ordnet ein Import zu –: Zahlung oder Erstattung), lassen
  sich **Betrag, Bankkonto und Buchungsdatum** nicht ändern (`_assigned_bank_change_blocker`, `BankChangeRefused` → 400:
  „Dieser Bankbewegung ist eine Rechnungszahlung zugeordnet (Rechnung …). Bitte zuerst die Zuordnung an der Rechnung entfernen,
  dann … ändern.“, bei Erstattungen der Wortlaut mit der Stornorechnung) – mit und ohne Editor. Verglichen wird mit dem Stand
  vom Beginn der Transaktion (Betrag in Minor Units); ein unverändert zurückgeschickter Wert ist kein Verstoß; Empfänger, Text,
  Währung und Steuerdatum bleiben frei. Geprüft wird NACH den Schreibschritten: Wer im selben Speichern alle Zeilen der Zahlung
  entfernt, darf den Betrag mit ändern (`tests/test_bank_change_assigned_payment.py`). Die Zeilen der Zahlungssätze selbst
  lassen sich im Editor weiterhin ändern (`docs/aufgaben.md`)

### Diagnose-MCP-Server
- `tools/mcp_ubuchhaltung.py`: nur Standardbibliothek, stdio; Claude Code startet ihn lokal,
  er fragt eine laufende Instanz (z. B. den Docker-Container) per HTTP ab
- Lesend per Konstruktion: genau ein SELECT ohne `;` über `/execute_sql` (ohne `write`, also im Lesemodus der Konsole – der
  Wächter hier bleibt trotzdem die erste Schicht), nur lesende
  `pragma_`-Funktionen (`_READ_PRAGMAS`), kein WITH/RECURSIVE/zeroblob/randomblob; Seiten nur
  aus `_PAGE_ALLOWLIST` samt erlaubten Query-Schlüsseln – diesen Lesewächter (`check_select`,
  `check_page_path`) nicht aufweichen; neue Seiten und Schlüssel nur für GET-Handler, die
  nichts schreiben (Nachweis im Test). `/backup/download` steht bewusst nicht darin: Es liefert den ganzen Datenbestand
  (Gigabyte-Größe). `/receipts/search` und `/transactions/search` stehen bewusst nicht darin: JSON-Suchen der Seiten, keine
  Anzeigeseiten; dasselbe liefert die SQL-Abfrage. `/bwa/pdf`, `/bwa/csv`, `/euer/pdf`, `/susa/pdf`, `/susa/csv` und `/liquiditaet/pdf` stehen nicht darin (Dokumente).
  Ebenso nicht: `/receipts/suggestions` (JSON-Vorschläge der Seite eines Belegs) und
  `/receipts/read` (ein POST: liest die Datei eines Belegs und schreibt die Lesung)
- Zugangsdaten aus der Windows-Anmeldeinformationsverwaltung, keyring oder Umgebung, nie im Repo; über `http://` gehen sie nur
  an den eigenen Rechner, sonst nur mit `UBUCHHALTUNG_MCP_ALLOW_HTTP`. Registrierung nur
  lokal (`claude mcp add --scope local`), Einrichtung in `DEPLOYMENT.md`

### Demo-Modus & Screenshots
- `UBUCHHALTUNG_DEMO=1` (`demo.py`): Demodaten liegen **ausschließlich unter
  `data/users/demo/`** und werden nur geladen, wenn dieses Verzeichnis fehlt oder leer
  ist und es keinen Benutzer `demo` gibt – diese Garantie nie aufweichen. Solange die
  Variable gesetzt ist, arbeitet jeder Besucher als `demo` (kein Administrator); `demo`
  ist wie `tests` ein reservierter Benutzername. Demodaten (`demo_seed.py`,
  `seed_data/test/demo_scenario.json`) entstehen über die App-eigenen Code-Pfade,
  relativ zum Startdatum; pytest-Testdaten bleiben davon getrennt. Der Import aus der WISO-Datenbank ist im Demo-Modus
  abgeschaltet (Route 403, Handler und Seite mit demselben Satz, `wiso_upload.DEMO_REFUSED`): keine Dateien eines Besuchers
  auf dem Server. Ebenso das Hochladen von Belegen auf der Seite *Belege* (`POST /receipts/upload`: 403 vor dem Lesen,
  `upload_handler.RECEIPTS_DEMO_REFUSED`; die Seite zeigt den Satz statt der Ablage) und – seit 2.6.7 – das Hochladen von
  Kontoauszügen auf der Seite *Buchungen* (`POST /upload_receipts`: 403 vor dem Lesen, `upload_handler.STATEMENTS_DEMO_REFUSED`; die
  Seite zeigt den Satz statt der Ablage, `POST /confirm_transactions` antwortet dort ebenfalls 403 – ein Import aus der Zeit davor lässt
  sich im Demo-Modus nicht mehr bestätigen, gelöscht wird nichts; `tests/test_statement_upload_demo.py`) Seit 2.6.8 nimmt die App im
  Demo-Modus **gar keine Datei eines Besuchers** mehr an: auch der Logo-Upload (`POST /masterdata/contacts/upload-logo`: 403 vor dem
  Lesen, `handlers.LOGO_DEMO_REFUSED`) und der Import der Textexporte (`POST /wiso/import`: 403 vor dem Lesen,
  `handlers.WISO_TEXT_DEMO_REFUSED`) sind abgeschaltet, die Seiten zeigen den Satz statt der Dateiauswahl. Ein neuer Upload-Weg braucht
  dieselbe Sperre (`tests/test_demo_uploads_closed.py` zählt die Wege auf).
- **Der große Bestand (seit 2.5.10):** Neben dem von Hand gepflegten Szenario (`demo_scenario.json`, bleibt unverändert) erzeugt
  `demo_history.build_history(today, profile)` mehrere Jahre – rein, ohne Datenbank, Zufall und Uhr (Auswahl je Monat über
  `zlib.crc32`, Tabellen in `seed_data/test/demo_history.json`, Kunden und Artikel aus den Test-Seeddaten). Profil `full` =
  drei volle Vorjahre und das laufende Jahr (≈ 300 Rechnungen; `demo._seed` → `load_demo_scenario(db)`), `test` = ein Vorjahr.
  `DemoScenario(db, today, extra=…, documents_days=…)` hängt die Vorgaben an die Listen des Szenarios an (ohne `extra` entsteht
  genau das Szenario) und lädt alle Rechnungsereignisse über EINE Warteschlange nach Datum (Anlegen, Zahlung, Storno,
  Ersatzrechnung, Erstattung, Angebot umwandeln): Die Nummern entstehen je Jahr in Datumsfolge, die Rechnungen des Erzeugers
  über den Weg der Maske (`handlers._invoice_fields`, `allocate_invoice_number`, `set_invoice_status`). PDF/E-Rechnung gibt es
  nur für die letzten 30 Tage (`documents_days`), und **in den letzten 14 Tagen legt der Erzeuger keine Rechnung an** – das
  E-Rechnungs-Beispiel (`tools/make_einvoice_samples.py`) bleibt die jüngste 19-%-Rechnung des Szenarios. Das erste Jahr ist ein
  Kleinunternehmerjahr; drei Bankbewegungen im laufenden Monat bleiben absichtlich offen (der DATEV-Export des laufenden Jahres
  fragt deshalb nach, abgeschlossene Jahre gehen ohne Hinweis durch). Für abgeschlossene Jahre bleiben Prüfliste, Splits,
  Übertragsprüfung und DATEV-Export ohne Befund – ändert sich der Erzeuger, die DATEN anpassen, nie die Prüfregeln
  (`tests/test_demo_history.py`). `load_test_seed_data(include_dated=False)` lässt die fest datierten Buchungen, Anlagen, Angebote und
  Arbeitszeiten weg (der Demo-Start nutzt es), `convert_quote_to_invoice(quote_id, today=…)` nimmt den Tag der Demodaten
- README-Bilder neu erzeugen: `node tools/screenshots.mjs` → `docs/screenshots/`
  (hell + `*-dark.png`, README bindet beide per `<picture>` ein)

### README & öffentliche Texte
- Oben im README steht, was Besucher brauchen (Zweck, Schnellstart, Überblick);
  Technik weiter unten. Grenzen ehrlich benennen, nichts versprechen
  (keine Steuerberatung, keine GoBD-Zusicherung)
- Markennamen Dritter (z. B. WISO) nur zur Bezeichnung von Importformaten, mit
  Markenhinweis; keine vergleichenden Aussagen oder „Alternative zu …“
- Das Repo ist öffentlich: Strategisches, Rechtliches, Notizen zu anderen
  Projekten und private Aufgaben gehören ins private Repo
  `NobseVomBerg/uBuchhaltung-Private` – lokal neben dem Projektordner
  (`../uBuchhaltung-Private`), in Cloud-Sessions per add_repo. Übergaben zwischen
  Sessions liegen dort in `uebergaben/`, nicht in `data/` (gitignored, für
  Cloud-Sessions unsichtbar). Fehlt es, nicht danach fragen

### Kontoauszug-Import (CAMT.053 + PDF + CSV)
- Ein Weg für alle Formate: Parser in `importers/` liefern `BankStatement` (`base.py`) →
  `server/upload_handler.py` → Vorschau `server/import_preview.py` (Duplikate, Warnungen) → bestätigen
- CAMT (`importers/camt.py`): Storno-Richtung = `CdtDbtInd` (ISO 20022); Saldoprüfung exakt in
  Decimal/Minor Units, nie als float-Summe; DTD/Entities werden abgelehnt (der XML-Wächter liegt seit 2.6.0 in
  `importers/safe_xml.py`, `parse_untrusted_xml`; die Belegerkennung nutzt dieselbe Stelle), ZIP nur im Speicher mit
  Grenzen, Beträge ≥ 10¹² sind Fehler – diese Schutzmaßnahmen nicht aufweichen
- **Nichts still falsch oder unvollständig (seit 2.3.9):** Ein CAMT-Auszug, der irgendwo (Konto, gebuchter Umsatz, verwendeter
  Saldo) eine andere Währung als Euro nennt, liefert keine Umsätze und nur den Befund `foreign_currency`
  (`_statement_currency`, `BankStatement.currency`); ein Einzelbetrag einer Sammelbuchung in anderer Währung teilt nicht auf.
  **Nur `TxDtls/Amt` teilt eine Sammelbuchung auf** (`_tx_amount`), die Beträge unter `AmtDtls` bewusst nicht: So las der
  Import solche Auszüge immer, und ein erneuter Import fände seine Summenzeile sonst nicht wieder – nicht ändern, ohne die
  Duplikaterkennung mitzuziehen. **Saldoprüfung für alle Formate:** `pdftext.find_balances` (Zeilen „alter/neuer
  Kontostand“, lineare Laufzeit – der Text kommt aus hochgeladenen PDFs) und `plausibility.check_balance` (ganze Cent);
  `balance` = stimmt nicht, `no_balance` = Saldo nicht gefunden (auch CAMT ohne OPBD/CLBD) – nichts wird geraten.
  **Volksbank-PDF:** Das Jahr steht nur im Kopf (`header_year`, fehlt es: `no_year`); läge eine Buchung damit nach dem
  Erstellungsdatum (`latest` = „erstellt am“), gilt das Vorjahr (`_booking_date`); ein Datum, das es nicht gibt, ist `None`
  (Befund `date`, in der Vorschau `incomplete`) – nie das heutige. Ein Buchungskopf ohne lesbaren Betrag ergibt `unparsed`,
  Saldozeilen landen nicht im Verwendungszweck. `daterange` meldet auch ein Datum mehr als 31 Tage NACH dem Belegdatum
  (`MAX_DAYS_AFTER_DOCUMENT`). Die Belegwarnungen eines PDF gehen in die Vorschau (`upload_handler`), ein als Auszug
  erkanntes PDF ohne Umsätze wird als Warnung gemeldet statt als abgelegter Beleg. Die Texte stehen fest in
  `FILE_WARN_LABELS` (`server/pages_transactions.py`). Die Saldosuche ist an 55 echten
  Volksbank-Auszügen eines Kontos (2022–2026) bestätigt: beide Saldozeilen überall gefunden, die Rechnung geht auf den Cent
  auf, kein Befund. Nicht gemessen: Soll-Saldo, einseitiger Auszug, Januar-Auszug mit Dezember-Buchungen (`docs/aufgaben.md`).
  Reine Strichzeilen (Rahmenzeichen U+2500–U+257F, seit 2.3.13) gehören zu keiner Buchung – neuere Auszüge rahmen „neuer
  Kontostand“ damit ein
- **DKB-CSV (seit 2.5.0, `importers/dkb_csv.py`):** Die Umsatzliste der DKB, zwei Formate – neu (`Buchungsdatum`, Gegenseite in zwei
  Spalten, Datum `TT.MM.JJ`, UTF-8 mit BOM) und alt (`Buchungstag`, eine Spalte, `TT.MM.JJJJ`, oft Windows-1252, auch mit Komma statt
  Semikolon und mit verlorenem Umlaut) –, **erkannt an der Datei, nie konfiguriert**: Kopfzeile = erste Zeile, deren erste Zelle
  `Buchungsdatum`/`Buchungstag` heißt (ihr Trennzeichen wird an ihr gezählt, nicht an den Beträgen); die Spalten werden **nach
  Bedeutung** gefunden (`MEANS`), nie am genauen Namen oder an einem Umlaut – Pflicht sind Betrag, Verwendungszweck und Gegenseite
  (beide Seiten oder eine einzelne Spalte, nie nur eine der beiden Seiten); die Liste einer anderen Bank mit derselben Kopfzeile
  wird genauso gelesen, eine andere Währung als Euro im Namen der Betragsspalte abgelehnt. **Beträge nur über `money.parse_grouped_amount`:** der
  Punkt ist IMMER ein Tausendertrenner (die DKB lässt bei runden Beträgen die Nachkommastellen weg: `-15.000` ist −15000) – bewusst
  anders als `parse_amount` (`1.500` ist dort 1,5); Leerraum gilt nur am Rand und vor `€`/`EUR` (`12 34` ist kein Betrag); nicht
  angleichen, nie `float()`. Befunde am Beleg: `no_balance` immer (kein
  Anfangssaldo in der Datei), `skipped_status` (Status „Vorgemerkt …“ übergangen), `unparsed` (Datum oder Betrag nicht lesbar: nicht
  importiert; still übergangen wird nur die Fußzeile – kein Datum UND nichts in der Betragsspalte), `no_iban` (setzt
  `check_statement`; die IBAN steht im Vorspann). **Eine verrutschte Zeile wird nie gelesen** (`unparsed`): mehr Zellen als die
  Kopfzeile – auch leere, ein Trennzeichen ohne Anführung schiebt sonst die Bankleitzahl in die Betragsspalte – oder zu wenige für
  Betrag, Zweck und Gegenseite. Ein Datum vor 1990 (`MIN_YEAR`) oder mehr als 31 Tage nach heute gilt als nicht lesbar. Diese
  Prüfungen nicht lockern – ein falscher Betrag wiegt schwerer als eine Zeile, die der Nutzer nachträgt.
  `document_date` bleibt `None`: Ohne Belegdatum entfällt `daterange` – eine Umsatzliste hat keines, und jedes andere gäbe bei
  einem Jahresexport Fehlalarme. Nur im Speicher gelesen (`upload_handler._collect_csv`, nie auf die Platte): höchstens 20 MB, 100 000
  Zeilen hinter der Kopfzeile, Kopfzeile in den ersten 100 Zeilen, 100 Spalten, 2000 Trennzeichen je Zeile der Datei
  (`_lines`: bricht ab, bevor das csv-Modul Millionen Zellen anlegt); `csv.field_size_limit` wird nie umgestellt. Was keine
  DKB-Umsatzliste ist oder unlesbar bleibt, ist eine Warnung (`other_files`, `status: 'warning'`) und nie ein abgelegter Beleg; die
  Meldungen (`DkbCsvError`) sind feste Sätze und nennen **keinen Feldwert der Datei** (Spaltennamen der Kopfzeile höchstens auf 40
  Zeichen gekürzt). Nur an erfundenen Beispielen geprüft, nicht an einer echten Datei (`docs/aufgaben.md`)
- **Einzelne Umsätze auswählen (seit 2.5.0, alle Formate, nur bei einem gemischt genutzten Konto):** Die Vorschau nennt je Umsatz `index`
  (Position in `import_preview.file_transactions` – DIE eine Stelle, die die Liste liest; Vorschau und Import durchlaufen dieselbe
  Liste in derselben Reihenfolge) und `importable` (weder Duplikat noch unvollständig), je Beleg `selectable`. **Gekoppelt an das
  Kennzeichen am Bankkonto** (`Accounts.BusinessOnly`, Schema 2005): `selectable` = Konto gefunden UND
  `Database.account_allows_selection` (streng gelesen: nur die ganze Zahl 0 = gemischt genutzt; **Standard und jeder andere
  gespeicherte Wert – NULL, Text, 2 – ist „reines Geschäftskonto“, die Kasse nie**; `db.accounts.is_business_only`). Ohne Auswahl
  (reines Geschäftskonto) sieht die Karte aus wie vorher; mit ihr: Kästchen je Zeile (Standard: jede importierbare angehakt, Duplikate und
  unvollständige deaktiviert), Suchfeld, „Alle“/„Keine“ (nur auf sichtbare, auswählbare Zeilen), „Importieren (n ausgewählt)“, bei 0
  gesperrt. `/confirm_transactions` nimmt das Feld `selection` (JSON-Objekt `{"<file_index>": [<index>, …]}`): ein Beleg MIT Eintrag
  legt nur die genannten Umsätze an, einer OHNE wird ganz importiert. **Streng und vor dem ersten Schreiben geprüft** (`_import_selection`):
  kein JSON-Objekt, doppelter Schlüssel, ein Schlüssel, der keine kanonische ASCII-Ganzzahl und kein `file_index` des Imports ist, ein Wert, der
  keine Liste ganzer Zahlen (kein `bool`, keine Kommazahl) im Bereich der Umsätze ist, doppelte Positionen, das Feld mehrfach → `400`
  „Ungültige Auswahl“; ein Eintrag für einen Beleg ohne gefundenes Konto oder ohne erlaubte Auswahl (auch für einen anderen Beleg
  desselben Imports) → `400` „Für ein reines Geschäftskonto wird der Auszug vollständig importiert. Bitte die Vorschau verwerfen
  und die Datei erneut hochladen.“. Nichts ist dann importiert. **Die
  Duplikat-Zählung bleibt, wie sie war:** Jede Zeile wird eingeordnet (`dup_seen`/`dup_db_counts` rücken für jede Zeile vor, gewählt
  oder nicht), danach Duplikat → `skipped`, unvollständig → `skipped_invalid`, nicht gewählt → `skipped_unselected`, sonst angelegt;
  ein gewähltes Duplikat wird nie doppelt angelegt, der Text-Backfill bleibt. Der Beleg gilt danach als importiert (nicht gewählte
  Umsätze sind verworfen). Der Client schickt `selection` nur für Karten mit Kästchen
- Valuta, EndToEndId, Mandatsreferenz, Gläubiger-ID haben im Modell noch keinen Platz (Schemaänderung offen)
- **Duplikate** = Konto + Buchungsdatum + Betrag, ohne Text (`check_booking_exists`: CAMT und PDF
  schreiben den Text verschieden, der WISO-Tabellenexport überschreibt ihn), zählbasiert **je Datei**:
  Der Bestand enthält, was frühere Dateien desselben Laufs angelegt haben – über mehrere Dateien gilt
  das Maximum, nicht die Summe. „Alle importieren“ (`handle_confirm_import`) entspricht damit den
  Einzelimporten nacheinander; die Vorschau (`build_import_preview`: `dup_source` = `db`/`upload`)
  rechnet dasselbe, ohne zu schreiben. Grenzen ohne Bankreferenz im Modell: CAMT-Sammelbuchung gegen
  PDF-Summenzeile; zwei verschiedene Auszüge desselben Tags mit je einem echten, gleichen Umsatz.
  Die Vorschau kennzeichnet ein Duplikat aus einer anderen Datei, dessen Verwendungszweck (der Text, den
  der Import speichert) zu keinem Umsatz früherer Dateien passt (`text_differs`; passend = gleich, Anfang
  des anderen oder leer, verglichen ohne Leerraum, mit `casefold` und ohne führendes „Storno:“): nur ein
  Hinweis, der Import bleibt beim Maximum.
  Ein Umsatz ohne Datum oder Betrag ist in Vorschau und Import gleich behandelt – nicht importierbar
  (Befund `incomplete`; je Datei `skipped_invalid`), weder neu noch Duplikat

### WISO-Datenübernahme
- Zwei Wege: die **Textexporte** (`db/wiso_import.py`, CSV) und die
  **WISO-Datenbank selbst** (`db/wiso_fdb_import.py` + `importers/wiso_fdb/`)
- Die Firebird-Dateien (`DB1.FDB` = Mandant, `DB0.FDB` = Standard-Kontenrahmen)
  sind unverschlüsselt und werden **ohne Firebird-Installation** gelesen –
  reines Python, kein Treiber, keine DLL
- **Satzversionen (seit 2.3.10, `importers/wiso_fdb/ods.py`):** `OdsFile.rows` liefert je Satz den letzten **abgeschlossenen**
  Stand, nicht die oberste Version – war WISO beim Kopieren geöffnet oder wurde es nicht sauber beendet, kämen sonst geänderte
  Werte statt der gültigen, ein nur vorläufig gelöschter Satz fehlte, ein nie abgeschlossener neuer käme mit. Zustand einer
  Transaktion aus den TIP-Seiten (Seitentyp 3, über `RDB$PAGES`; 2 Bit je Transaktion, 3 = abgeschlossen; Transaktion 0 gilt
  als abgeschlossen – die Systemsätze tragen sie). Von der obersten Version über die Rückzeiger zurück bis zur ersten
  abgeschlossenen; trägt die neuere Version `R_DELTA`, ist die Vorversion ein Differenzstrom (`apply_diff`, nach dem
  RLE-Entpacken). Kein Rückzeiger oder abgeschlossen gelöscht: den Satz gibt es nicht. Gezählt wird, nichts verschwiegen
  (`version_stats` → Ergebnis `uncommitted_records`, `unreadable_versions` [Kette beschädigt: der Satz fehlt, kein Blocker],
  `unknown_tx_state` [TIP nicht lesbar: gelesen wie in der Datei]; feste Hinweise auf *Sonstiges*). Geprüft gegen echte
  Firebird-3.0-Dateien mit offener Transaktion (`tests/fixtures/firebird/`, mit Firebird erzeugt – Fragmente, Blobs,
  Differenzketten); ungeprüft: `rhd_long_tranum` und mehrere TIP-Seiten an einer echten Datei. `tests/fdb_builder.py` schreibt
  eine TIP-Seite (Transaktion 1 abgeschlossen, 2 offen für `add_open`). Diese Regel nicht aufweichen
- Der Datenbankweg ist genauer: `MOV_FINACC_ACCRECORDS.ACCOUNTINGID` nennt die
  Zusammengehörigkeit von Teilbuchungen ausdrücklich (→ `Bookings.SourceGroup`),
  liefert die Anlagenverwaltung mit und umfasst alle Jahre
- WISO rechnet intern in **SKR03**. Die SKR04-Nummer eines Kontos kommt in dieser Reihenfolge (`WisoDatabase.chart()`, seit 2.2.5):
  `BAS_FINACC_PLAN.SKR04` (nur die alten Kontenpläne; Mandant schlägt Standard; **wird nie überschrieben**) → die von WISO
  **gespeicherte Nummer** (`FINT_EXTERNDEFS.EXTERNVALUE` bei `EXTERNKIND = 82`, jüngster Plan = höchste `FINTABLEID`; nur
  Sachkonten: `ACCOUNTNO` < 10000, Wert 1–9999; nicht für Konten aus `KNOWN_EQUIVALENTS`) → das Basiskonto
  (`FINT_ACCOUNTS.BASEACCOUNTNO`, nur noch Rückgriff) → `KNOWN_EQUIVALENTS`. Eigene Unterkonten (z. B. je Fahrzeug: 4532 → 6532)
  behalten so ihre Nummer – die Trennung ist fachlich nötig, „Basis + Abstand“ ist nicht WISOs Regel. Konten ohne Zuordnung
  werden **gemeldet, nicht geraten**; fehlende SKR04-Konten legt der Import mit WISOs Bezeichnung an. Ein Bestand aus einem
  Import bis 2.2.4 (Sätze auf dem Basiskonto) bleibt, wie er ist: Der Re-Import erkennt ihn an der Quellgruppe, legt keinen
  zweiten Satz an und zählt ihn (`subaccount_rows`); eine vorhandene Anlage behält ihr Konto (`COALESCE(COA_ID, ?)`). Ist das
  Unterkonto eines Zahlungskontos in der App kein Zahlungskonto, wird das gemeldet (`liquid_subaccounts`: gelesen als
  Umbuchung, nicht in der EÜR) – keine Sonderbehandlung
- **Klammer eines Belegs ist die Beleg-Nr. + Datum**, nicht die `SourceGroup`:
  WISO vergibt je Verrechnungskonto-Buchung eine eigene `ACCOUNTINGID`. Die
  Quellgruppe ist damit feiner und greift nur, wo eine Beleg-Nr. fehlt
- **Vorzeichen und Form:** Beide Importe richten das Vorzeichen an den liquiden Seiten aus (eigene Konten
  und 1460; genau eine Seite liquide → Richtung der Geldbewegung; ein negativer Brutto bei genau einer
  liquiden Seite zählt als `negative_gross`, keine oder zwei liquide Seiten: der Rohbetrag bleibt und
  zählt nicht) und schreiben die Form mit: Konto liquide `liquid_first`,
  Gegenkonto liquide `purpose_first`, beide `mirror_coa` (der **Rohbetrag**, positiv = Konto im Soll – der
  Betrag neuer Importe ändert sich dadurch nicht, ein Re-Import findet seine Zeilen), **keine liquide Seite
  `transfer`** mit dem Rohbetrag (WISO meint „Konto im Soll“ – auch bei leerem oder unbekanntem Gegenkonto,
  nie `purpose_first`; die EÜR liest so einen Satz als Umbuchung, ohne Vorsteuerausweis).
  **Kein Import ändert die Form einer vorhandenen Zeile:** Der Re-Import der Datenbank (bekannte `SourceGroup`)
  und der des Textexports erkennen einen `mirror_legacy`-Spiegel wieder – mit gleichem oder umgekehrtem
  Vorzeichen (Beleg-Nr., Datum, Konto, Gegenkonto gleich, von der Datei nicht selbst beansprucht) –, legen
  keinen zweiten Satz an und lassen Form und Betrag, wie sie sind; sie zählen ihn (`legacy_mirrors`, Hinweis
  auf *Sonstiges*). Weicht eine andere gespeicherte Form von der des frischen Imports ab, wird das nur
  gemeldet (`form_conflicts`). **Zeilen auf Konten 1000–1099 aus 2.0.x** (dort als Kasse gelesen, also mit
  einem anderen Betrag gespeichert: „6815 an 1050“ −10, „1050 an 1800“ +30) erkennen beide Wege am Betrag der
  alten Regel wieder (Zahlungskonten einschließlich 1000–1099): der Re-Import des Textexports mit einer
  zusätzlichen Duplikatsuche (gleiche Beleg-Nr., Datum, Konto, Gegenkonto, jede Zeile höchstens einmal, nicht
  die, die eine andere Zeile der Datei mit ihrem eigenen Betrag beansprucht), die Übernahme CSV → Datenbank
  über `WisoBooking.legacy_amount` unter denselben Schutzregeln wie sonst. Kein zweiter Satz, Form und Betrag
  der Altzeile bleiben, die Übernahme füllt nichts; der Zähler heißt `legacy_stock_rows` (Hinweis auf
  *Sonstiges*)
- **Textimport** (`import_wiso_csv`): Kodierung in der Reihenfolge BOM → strenges UTF-8 → cp1252 (cp1252 dekodiert fast alles
  und darf deshalb nie zuerst stehen). **Beträge aus Dateien liest nur `money.parse_amount`** (seit 2.3.12; nie `float()`: das
  nähme `nan`, `inf`, `1e5`, `1_000` und Ziffern anderer Schriften an; Ausnahme: die DKB-CSV liest `money.parse_grouped_amount`, in der
  der Punkt immer ein Tausendertrenner ist – siehe *Kontoauszug-Import*): nur ASCII, deutsch `1.234,56` (Tausenderpunkte nur in
  Dreiergruppen) oder ohne Komma mit Punkt als Dezimalpunkt (`1.234` bleibt 1,234 – die bisherige Lesart), höchstens
  30 Zeichen und betragsmäßig unter einer Billion (`MAX_PARSED_AMOUNT`), sonst der Zeilenfehler „Ungültiger Betrag“. Der
  Betrag bleibt `Decimal`; der Steuerbetrag kommt aus `_included_tax`: exakt auf dem Brutto der Datei, Brutto × Satz /
  (1 + Satz), EINE kaufmännische Rundung auf den Cent – weder das Brutto noch ein Zwischenwert wird vorher gerundet (in
  Minor Units gerechnet und danach gerundet wiche er bei rund 0,4 % der Beträge um einen Cent ab). Die PDF-Auszüge (`importers/vbr.py`, `document_parser.py`) lesen ebenso und
  verlangen das Komma; ein nicht lesbarer Betrag ist dort `unparsed` bzw. „kein Betrag“. `money._to_decimal`/`to_minor`
  lehnen NaN und Infinity mit `ValueError` ab. Die Meldungen der Formaterkennung nennen **keine Feldwerte der Datei** (ein DATEV-Stapel trägt Berater- und
  Mandantennummer im Vorlauf; er wird am ersten Feld `EXTF`/`DTVF` erkannt und mit Klartext abgelehnt). `SCHLUESSEL` (ganze Zahl 1–9999) steht am
  Satz (`DatevKey`); der Steuersatz kommt zuerst aus der Tabelle der Regeln (`datev_keys.key_rate`: 401/101/121 Regelsatz, 402/102/122 ermäßigt,
  7/6/5/4 die anderen Sätze – im zweiten Halbjahr 2020 mit 16/5/19/7 %), sonst aus `TaxKeys` (2.3.0: 100, 120, 121 „Verwendung von Gegenständen“, 122
  ergänzt bzw. berichtigt, 490 ohne Satz). Ein Schlüssel, den beide nicht kennen, wird nicht geraten: Die Zeile kommt ohne Steuersatz und zählt
  in `unknown_tax_keys` (Hinweis auf *Sonstiges*). Die stille 19 % bei „4405 an 4400“ gilt **nur ohne Schlüssel** (leer oder 0): Eine Zeile
  mit einem Schlüssel, auch einem ohne Satz (490), behält, was er sagt. Der Import schreibt nur neue Zeilen – ein Duplikat bleibt, wie es ist,
  auch ohne Schlüssel
- **BU-Schlüssel und Nummern aus der WISO-Datenbank:** `DATEVVATKEY` (optionale Spalte; leer, 0 und alles außer 1–9999 ist keiner) steht beim neuen Satz
  am Satz (`WisoBooking.datev_key`), die Übernahme einer CSV-Buchung füllt ihn nur, wo er fehlt (`COALESCE`), und der Re-Import trägt ihn an der
  wiedererkannten Zeile einer bekannten Quellgruppe nach – **nur diese Spalte, nur wo sie leer ist, nur bei genauem Treffer** (`datev_keys_filled`;
  eine vom Nutzer geänderte Zeile bekommt nichts, ein vorhandener Schlüssel bleibt) – **und nur, wenn die vorhandene Zeile denselben Steuersatz
  trägt wie der WISO-Satz** (`_same_rate`: Prozentzahl, kein Satz = 0 %; bei der Übernahme gilt der Satz, den die Zeile nach dem UPDATE trägt –
  es füllt ihn, wenn Satz und Steuerbetrag leer sind): Sonst bleibt `DatevKey` leer, der Export leitet ihn aus dem Satz der App ab, und der Fall
  zählt in `datev_key_rate_mismatches` (Hinweis auf *Sonstiges*) – der Schlüssel gehört zu dem Satz, für den WISO ihn geschrieben hat, und
  der Nutzer kann ihn mit `update_booking_fields` geändert haben. Gleichen sich in einer Gruppe mehrere Zeilen in Datum, Konten und Betrag,
  bekommt der Satz zuerst die Zeile mit seinem Steuersatz: Ein Schlüssel hängt nie an der Zeile eines anderen Satzes. Berater- und Mandantennummer
  stehen in `SYS_FINACCSYSTEMS` (Satz `NAME` = „Datev-Format“; **nur** `NAME`, `CONSULTANT_NO`, `CLIENT_NO` – die Tabelle trägt auch Zugangsdaten
  anderer Schnittstellen; nichts davon wird geloggt) und gehen nur in die eigene Firma (Unternehmen), wenn deren beide Felder leer sind
  (`datev_numbers_set`) – **erst nachdem die Buchungen geschrieben sind** (scheitert der Import, bleiben die Stammdaten, wie sie waren); gibt es
  keine eigene Firma (als Unternehmen angelegt), nennt ein Hinweis, dass die Nummern nicht übernommen wurden (ohne sie zu nennen); fehlt Tabelle
  oder Spalte, passiert nichts (Hinweis, kein Blocker), und ein Lese- oder Dekodierfehler der Tabelle (`datev_numbers_unreadable`) gibt „keine
  Nummern“ und einen Hinweis ohne Fehlertext, nie einen Abbruch
- **CSV und Datenbank nacheinander:** Der Datenbank-Import (`_insert_wiso_bookings`) übernimmt eine CSV-Buchung
  (`entry` ohne `SourceGroup`, `AutoMirror`, `Account_ID`) nur, wenn Datum, Betrag, Konto, Beleg-Nr. **und ein
  nicht leerer Text** gleich sind (Leerraum/`casefold`; der latin-1 gelesene WISO-Text wird dafür nach cp1252 umgesetzt) und das
  Gegenkonto nicht widerspricht; gleiches Gegenkonto geht vor, dann die kleinste ID, jede Zeile höchstens einmal.
  Ein `mirror_legacy`-Spiegel mit umgekehrtem Vorzeichen wird übernommen, wenn Konto, Gegenkonto (beide bekannt),
  Beleg-Nr. und Text gleich sind und die Datenbank nicht selbst eine Zeile mit seinem Betrag enthält; Form und
  Betrag bleiben (nur die Quellgruppe kommt dazu). Füllt die Übernahme ein fehlendes Konto, ändern sich die
  Konten des Satzes – dann gilt für ihn die Form des Imports.
  Gleicher Schlüssel + Text bei verschiedenen Gegenkonten ist mehrdeutig → eine Zeile ohne Gegenkonto bleibt liegen.
  Grundsatz: **im Zweifel neu anlegen und melden – nie aufweichen**; ähnliche, nicht übernommene Buchungen stehen
  als `possible_duplicates` auf der Seite. Übernommen wird per UPDATE (im Replay-Log; `adopted_rows`); die
  Ersatzsuche ohne Konto nur bei gleichem Gegenkonto – und nicht, wenn sich zwei Sätze des Imports nur im Konto
  unterscheiden
- **Neu angelegte Sätze (seit 2.3.2):** Legt WISO Buchungen neu an (nach einem DATEV-Export oder einer Korrektur), stehen sie mit
  neuer `ACCOUNTINGID` und sonst gleichem Inhalt in der Datenbank – die alte Quellgruppe gibt es dort nicht mehr. **Verwaist** ist
  eine `entry` mit Quellgruppe, die in keinem Satz der Datei vorkommt (auch nicht in einem künftigen AfA-Satz, dessen Gruppe die Datei
  ja noch nennt); der Import liest immer die ganze Datenbank, es gibt keinen Zeitraumfilter – bekäme er einen, wäre jede Zeile
  außerhalb verwaist, und die Logik gehörte dort abgeschaltet. Jeder Satz mit **unbekannter**, nicht leerer Quellgruppe bekommt – vor
  der Übernahme aus dem CSV-Import und vor dem Neuanlegen – eine verwaiste Zeile, wenn eine passt. **Die Zuordnung steht fest, bevor die
  Hauptschleife läuft** (Abbildung Satz → Zeile, die Schleife schlägt nur nach) und **hängt nicht von der Reihenfolge der Sätze in der
  Datei ab**: Die Durchgänge (`_REGROUP_PASSES`) gehen über ALLE diese Sätze in fester Ordnung nach Inhalt, die Zeilen nach ID; jede
  Zeile dient höchstens einem Satz, jeder Satz bekommt höchstens eine Zeile. **Immer nötig:** gleiches Datum, Konto, Gegenkonto (leer =
  leer) und **gleicher Steuersatz** (`_same_rate`) – ein anderer Satz ist nie ein Treffer (der Satz wird neu angelegt, die alte Zeile
  bleibt verwaist). Dann nacheinander: (1) gleicher Betrag mit Vorzeichen und gleiche, nicht leere Beleg-Nr. (Leerraum zählt nicht), der
  Text darf abweichen (`regrouped_text_differs`; die Zeile mit gleichem Text geht vor); (2) gleicher Betrag, beide ohne Beleg-Nr.,
  gleicher nicht leerer Text; (3) gleicher Betrag, Zeile ohne Beleg-Nr., Satz mit einer in WISO nachgetragenen, gleicher nicht leerer
  Text (die Beleg-Nr. der Zeile bleibt leer); (4) nur Altbestand: Zeile der Form `mirror_legacy` mit dem umgekehrten Betrag, sonst wie
  (1) bis (3) – und nur, wenn kein Satz der Datei auf demselben Schlüssel (Datum, Konten, Beleg-Nr.) den Betrag der Zeile selbst trägt
  (dieselbe Sperre wie bei der CSV-Übernahme). **Kein Rückfall auf den Absolutbetrag** für jede andere Form; eine Zeile mit Beleg-Nr.
  passt nie zu einem Satz ohne, zwei verschiedene Nummern nie. Ein Treffer ist **kein neuer Satz**: Nur `SourceGroup` wird gesetzt
  (UPDATE im Replay-Log, „neu angelegten Satz wiedererkannt“), jede andere Spalte bleibt – Betrag, Form, Text, Beleg-Nr., Steuer,
  `ParentBooking_ID`, Empfänger; der Grundsatz „Kein Import ändert die Form einer vorhandenen Zeile“ gilt unverändert. Die Zeile zählt
  für den Rest des Laufs wie eine in ihrer bekannten Gruppe wiedererkannte (derselbe Rückgabeweg wie `recognize`: `skipped`, AfA-Plan,
  `legacy_mirrors`/`form_conflicts`, BU-Schlüssel nur wo leer). Gezählt wird `regrouped_rows`, bei abweichendem Text zusätzlich
  `regrouped_text_differs`; die Zusammenfassung nach dem Import nennt sie (`regrouped`, `orphans` in der Weiterleitung; angezeigt nur, wenn > 0).
  **Verwaiste Zeilen ohne Treffer** meldet der Lauf (`orphaned_rows_count`, die ersten 50 in `orphaned_rows`: ID, Datum, Betrag,
  Kontonummern, Text) und die Seite *Sonstiges* mit Links auf die Buchungen: Die Liste nennt Buchungen, deren Quelle (Quellgruppe)
  in der WISO-Datenbank nicht mehr steht – dort gelöscht oder mit anderem Inhalt neu angelegt. Sie zählen in den Auswertungen weiter mit,
  **gelöscht wird nichts**. **Eine Änderung innerhalb einer weiter bestehenden Buchungsgruppe (anderer Betrag, entfallene Teilbuchung)
  bemerkt der Import nicht: Die vorhandene Zeile bleibt, wie sie ist.** Grenzen: Die Suche braucht dieselben Konten (ein Bestand aus
  einem Import bis 2.2.4 auf dem Basiskonto eines Unterkontos findet keinen Treffer: neu angelegt, die Altzeile gemeldet; ebenso eine Zeile
  aus 2.0.x auf den Konten 1000–1099, die ihren Betrag nach der alten Kassenregel trägt – sie steht bis zum Bereinigen zweimal da). Der Import ist
  für EINE WISO-Datenbank je Bestand gedacht: Zeilen eines anderen Mandanten gelten als nicht mehr vorhanden und können bei gleichem
  Inhalt umgehängt werden – nicht unterstützt. Rechnet WISO den AfA-Plan eines schon importierten Jahres mit anderem Betrag unter neuer
  Gruppe neu, steht die alte AfA-Buchung in der Liste der nicht mehr vorhandenen Buchungen und die neue zusätzlich in den Büchern – bis
  zum Bereinigen zählt das Jahr doppelt. Die verwaisten Zeilen werden einmal gelesen und nach (Datum, Konto, Gegenkonto, Betrag) abgelegt (linear)
- Bedienung über *Sonstiges* → „Import aus der Datenbank". **Die Dateien kommen als Upload (seit 2.3.7,
  `server/wiso_upload.py`), einen Serverpfad nimmt der Import nicht mehr entgegen:** Der Browser legt `DB1.FDB` (Pflicht) und
  optional `DB0.FDB` je als rohen Body über `POST /wiso/fdb-upload?slot=client|standard[&upload=<Kennung>]` ab, das Formular
  nennt danach nur die Kennung (`upload`). Die Dateien liegen unter `<Datenordner>/upload_tmp/wiso_fdb/<Kennung>/` mit FESTEN
  Namen (der Dateiname des Browsers wird nie verwendet); die Kennung (`secrets.token_hex(16)`, geprüft gegen
  `[0-9a-f]{32}`, Ordner direkt unter der Wurzel und kein Link) ist die einzige Eingabe, die einen Pfad bestimmt. Vor dem
  ersten gelesenen Byte stehen Token, Demo-Modus, Slot und Kennung, Länge und freier Platz; geschrieben wird in eine
  `*.part`-Datei und erst nach der vollen Länge umbenannt. Je Benutzer wartet höchstens EIN Upload (`begin` räumt die
  anderen weg, außer einem mit frischem Marker `.importing`). **`handle_wiso_fdb_import` löscht den Zwischenordner in jedem
  Fall** (`finally: discard` – nach dem Import, bei Blocker, Fehler und jeder Ablehnung), und **kein Serverpfad geht in
  Antwort, Weiterleitung oder `wiso_fdb_result.json`** (`_without_upload_path`; `result['file']` ist `DB1.FDB`) – beides
  nicht aufweichen. Eine Datei, die sich nicht als WISO-Datenbank öffnen lässt (`result['unreadable']`), ist ein Fehler
  der Seite, kein „Import abgeschlossen“. Die Prüfung
  (`WisoDatabase.check`) bricht vor dem Schreiben ab, wenn die WISO-Version
  andere Tabellen/Spalten hat. **Der Import ist für einen leeren Bestand gedacht:** Ist `Bookings` nicht leer
  (`Database.wiso_import_stock`: `bookings` = alle Zeilen, `wiso_rows` = `entry` mit Quellgruppe), zeigt das Formular über dem
  Absenden einen Hinweis (ohne bzw. mit Zeilen aus einem früheren Import der WISO-Datenbank) und ein Pflicht-Kästchen
  (`confirm_existing`, Wert genau `1`); `handlers.handle_wiso_fdb_import` prüft das vor jedem Zugriff auf die Datei und importiert
  ohne Bestätigung nichts („Der Bestand ist nicht leer – bitte den Import ausdrücklich bestätigen.“ über den Fehlerweg der Seite).
  `Database.import_wiso_fdb` selbst prüft nichts (Tests, Demodaten und Skripte rufen es direkt auf)
- **Anlagen:** Anschaffungswert und Verkaufserlös stehen beide **netto** in den
  Büchern. Der Erlös trägt in WISO keine `INVENTORYID` – gemeinsam ist ihm mit
  dem Anlagegut nur der Abgangstext; ist er nicht eindeutig, bleibt das Feld
  leer statt falsch. **Anschaffungswert** (`_purchase_price`, seit 2.2.6): Summe der positiven Zahlungen plus der in WISO
  eingetragene Wert (`PURCHASEAMOUNTNET`), wenn er über 1,00 € liegt und nicht auf den Cent der Zahlungssumme gleicht – bis
  1,00 € ist er ein Behelf des Nutzers bei der Erstanlage und zählt nicht, bei Gleichheit ist dieselbe Anschaffung zweimal
  erfasst und zählt einmal; ohne Zahlungen wie bisher. Zählt der eingetragene Wert zusätzlich, warnt der Import je Anlage
  (Zusammensetzung); bleibt bei einer nicht abgegangenen Anlage ohne geplantes Jahr nach den gebuchten Jahren ein Rest über
  1,00 €, warnt er ebenfalls (`depreciation_rest_warnings`)
- **Vorausgeplante AfA (seit 2.2.5):** WISO schreibt den ganzen AfA-Plan sofort als Buchungssätze, ohne Kennzeichen für
  „geplant“. **Ein AfA-Satz mit Datum nach dem Importtag wird nie als Buchung angelegt** (Stichtag `today`, Standard: Ortsdatum;
  `future_depreciations_skipped`) – gebucht wird im jeweiligen Jahr in der App. AfA-Satz = Satz mit `INVENTORYID` und
  `INVENTORYAMORTID` oder `ACCOUNTINGKIND = 28` (gemessen an einer WISO-Version); Spalte da und leer, Art nicht 28 → kein
  AfA-Satz (Abgangsbuchung); kennt der Satz die Spalte nicht, zählt gleiche Anlage, gleiches Datum und gleicher Betrag einer
  Zeile der AfA-Tabelle. **Der Planstatus folgt der Buchung, nicht dem Datum:** Ein Planjahr (`AssetDepreciations`) ist
  `booked`, wenn sein AfA-Satz in den Büchern steht (angelegt, aus CSV übernommen oder in der bekannten Quellgruppe
  wiedererkannt), sonst `planned`; `_insert_wiso_bookings` führt den Status vorhandener Planzeilen in seiner Transaktion mit
  (`BEGIN IMMEDIATE`, die Planjahre werden darin gelesen; auch bei `with_assets=False`), `_insert_wiso_assets` legt neue
  Planzeilen nach derselben Menge an, hebt sonst nie und senkt nie. Reihenfolge im Lauf: Buchungen vor Anlagen. **Hat die App
  ein Jahr selbst gebucht** (`Booking_ID` zeigt auf eine vorhandene Buchung), bleibt die Planzeile unverändert und der AfA-Satz
  der Datenbank wird nicht angelegt (`depreciation_conflicts`). **Bekannte Quellgruppe:** Ein Satz wird nie neu angelegt – mit
  einer Ausnahme: Fehlt der Gruppe für das Jahr jede Zeile und führt die eindeutig zugeordnete Anlage das Jahr als `planned`,
  wird der AfA-Satz nachgetragen. Hat die Gruppe im Jahr eine Zeile, die nicht genau passt (vom Nutzer geändert), gilt das
  Jahr als gebucht (Nachweis; jede Zeile für höchstens einen Satz, genaue Treffer zuerst). Künftige AfA-Buchungen aus einem
  Import bis 2.2.4 bleiben und zählen als gebucht (`future_depreciations_kept`). Zugeordnet wird die Anlage über die
  Inventarnummer, nur wenn sie eindeutig ist – sonst Warnung, kein Abgleich. Eine Planzeile, die der Buchungsschritt auf
  `booked` hebt oder die neu als `booked` entsteht, trägt den **Betrag der Bücher** (Wirkung der AfA-Sätze des Jahres auf
  das Anlagekonto, nie unter 0); der Planwert aus WISO aktualisiert nur `planned`-Zeilen, eine `booked`-Zeile gleicht allein
  der Buchungsschritt an den Betrag der Bücher an. Weicht der Plan der App (`depreciation_plan`) in einem geplanten Jahr um
  mehr als 1,00 € von WISOs Wert ab, meldet der Import es je Anlage (`depreciation_plan_mismatches`). Diese Regeln nicht aufweichen – jede Lockerung führt zu doppelter oder
  fehlender AfA
- **Rechnungen:** der Positionstext steht **nicht** bei der Rechnungsposition,
  sondern als RTF-Blob (`ARTDESCR`) an der Auftragsposition, auf die
  `ORDPOSID` zeigt – dort hat der Nutzer ihn für den Beleg angepasst.
  Einheiten kommen als UN/ECE-Code aus `SUP_ARTICLES_UNITS.OPENTRANSCODE`.
  Kunden behalten ihre WISO-Nummer; Sammelposten („(alle)“) tragen eine
  nicht-positive Id und sind keine Kunden

### Rechnungsdokumente (PDF + XRechnung)
- Ablage: `<Datenordner>/invoices/<Jahr des Rechnungsdatums>/<Rechnungs-Nr.> <Kunde>.pdf`
  (z. B. `R26003 Malermeister Pinsel.pdf`); die XRechnung trägt denselben Namen
  mit `.xml`. Namensschema und Ablage liegen zentral in `export/invoice_files.py`
- **Hybride E-Rechnung (ZUGFeRD 2.x / Factur-X, Profil EN 16931):** jede neu
  erzeugte Rechnungs-PDF ist PDF/A-3b mit eingebetteter `factur-x.xml` (CII,
  `/AFRelationship /Alternative`). Datenfluss: DB → `export/einvoice_model.py`
  (neutrales Modell, Beträge in Minor Units, Steueraufschlüsselung als Liste) →
  `cii_invoice.py` (eingebettet) und `xrechnung_invoice.py` (UBL, Download).
  Der PDF/A-Aufbau liegt in `export/pdfa.py` und gilt für **alle** PDFs
  (Angebot, Stundenzettel …); nur Rechnungen bekommen das XML
- **Schriften:** Liberation Sans (2.x, OFL 1.1) aus `export/fonts/` – metrisch
  = Helvetica, daher Layout unverändert; als Untermenge per `fontTools`
  eingebettet, Content-Streams bleiben WinAnsi. sRGB-ICC für den OutputIntent
  erzeugt `tools/make_srgb_icc.py` (Normwerte, keine Fremdlizenz)
- **Validierung:** CI-Job `einvoice` (`tests.yml`) erzeugt per
  `tools/make_einvoice_samples.py` Beispiele aus den Demodaten und prüft sie mit
  Mustang (feste Version, SHA-256 gepinnt; XSD/Schematron/veraPDF); die Dateien
  hängen als Artefakt am Lauf. Mustang-Version nur mit neuer Prüfsumme ändern
- **Leistungsdatum ist Pflicht** (§ 14 UStG): Formular `required`, vorbelegt mit dem Rechnungsdatum,
  Server lehnt Speichern ohne ab (`handle_invoice_save`; nicht bei Angeboten, nicht bei bereits
  versendeten Rechnungen/Importen); steht im PDF-Kopfblock und in CII/UBL. Angebot → Rechnung setzt es auf das Rechnungsdatum
- **Kopfblock im PDF wie im Eingabeformular (seit 2.5.2):** Reihenfolge und Bezeichnungen oben rechts folgen der Maske –
  Rechnung: Rechnungsdatum, Leistungsdatum, Rechnungs-Nr., Kunden-Nr. (dann „Fassung“ bei einer berichtigten); Angebot: Datum,
  gültig bis, Angebots-Nr., Kunden-Nr. (in beiden die Daten zusammen, dann die Nummern). Was fehlt, bekommt keine Zeile. Ändert sich die Maske, zieht das PDF mit
  (`tests/test_pdf_header_block.py`; die Beschriftungsspalte `META_LABEL_X`…`META_VALUE_X` fasst die längste Bezeichnung).
  Abgelegte Dokumente bleiben, wie sie sind
- **Hinweis auf § 19 UStG (seit 2.3.3):** Ein Beleg ohne Steuerausweis (`Invoices.TaxRate` = Platzhalter −1, Häkchen
  „Umsatzsteuer ausweisen“ aus) trägt den Hinweis auf die Steuerbefreiung für Kleinunternehmer automatisch – Rechnung,
  Stornorechnung und Angebot. EIN Wortlaut an EINER Stelle (`export/einvoice_model.EXEMPT_REASON_KLEINUNTERNEHMER`) für PDF
  (Zeile unter dem Summenblock, `draw_item_table(…, tax_note=…)`), CII (BT-120) und UBL. Ob das PDF ihn druckt, entscheidet
  `small_business_note`: nur beim Platzhalter (nicht bei 0 %), und nur dann nicht, wenn ein gedruckter Freitext des Belegs –
  Rechnung: Zahlungshinweis; Angebot: Einleitung, Schluss – das Gesetz ausdrücklich zitiert („§ 19“/„§ 19a“, höchstens
  „Abs./Satz n“ dazwischen, direkt gefolgt von „UStG“/„Umsatzsteuergesetz“). Geprüft wird der Text, wie das PDF ihn druckt
  (Richtext erst durch `sanitize_richtext`); Positionstexte, das Wort „Kleinunternehmer“ allein oder „§ 19 BGB“ unterdrücken
  nichts. Ein fehlender Hinweis wiegt schwerer als ein doppelter: die Erkennung nicht lockern. Abgelegte Dokumente bleiben,
  wie sie sind. **Das Häkchen eines neuen Belegs** (ohne Quelle: kein Bearbeiten, Kopieren, Umwandeln, Neu-Ausstellen) folgt
  der jüngsten gewöhnlichen Rechnung (`Database.last_document_shows_tax`: keine Stornorechnung, keine Ersatzrechnung, kein
  Angebot; ohne eine solche ist es an); startet es abgehakt, sagt die Maske das sichtbar
- **Pflichtangaben (seit 2.3.5, `export/einvoice_model.py`):** **Steuernummer und USt-IdNr.** der eigenen Firma werden an EINER Stelle
  unterschieden (`tax_id_kind`: eine vorangestellte Bezeichnung wie „USt-IdNr.“ oder „St.-Nr.“ fällt weg; USt-IdNr. ist nur, was –
  ohne Leerzeichen/Bindestriche/Punkte – mit einem bekannten Länderpräfix beginnt und dessen Format hat, z. B. DE + 9 Ziffern;
  alles andere ist eine Steuernummer, auch „HRB 12345“ oder ein zu kurzes „DE12345678“) – für die Momentaufnahme der Rechnung wie für den Rückgriff auf den Kontakt, in CII (`VA`/`FC`), UBL (`VAT`/`TAX`) und im
  PDF-Fuß („USt-IdNr.:“ bzw. „Steuer-Nr.:“; der Fuß liest die Momentaufnahme, den Kontakt nur, wenn sie leer ist – Rechnung und Angebot).
  Hat der Verkäufer nur eine Steuernummer, steht sie zusätzlich als Verkäuferkennung (BT-29, `Party.identifier`) in CII und UBL –
  ohne sie lehnt der Validator die E-Rechnung ab (BR-CO-26); mit USt-IdNr. gibt es kein BT-29.
  Eine Käuferangabe, die keine USt-IdNr. ist, geht nicht in die E-Rechnung. **Skonto** (`Invoices.SkontoDays`/`SkontoPercent`,
  `parse_skonto`: Tage 1–365, Prozent zwischen 0 und 100 mit höchstens zwei Nachkommastellen – geprüft am Text bzw. an der Zahl,
  BEVOR etwas umgerechnet wird (ein Wert wie `1e9999999` darf den Server nicht beschäftigen); beim Speichern einer nicht eingefrorenen
  Rechnung geprüft, nur bei positivem Brutto und gültigem Rechnungsdatum, die Frist darf nicht nach der Fälligkeit enden) steht im PDF als Absatz nach dem Zahlungshinweis (Frist ab
  Rechnungsdatum, Abzug vom gedruckten Brutto in EINEM Schritt halb auf den Cent gerundet – `skonto_amount`) und in der E-Rechnung als
  erste Zeile der Zahlungsbedingungen in der Schreibweise der XRechnung (`#SKONTO#TAGE=7#PROZENT=2.00#` + Zeilenumbruch, dann der
  Freitext); die Stornorechnung trägt nie Skonto; eine Skonto-BUCHUNG gibt es nicht (`docs/aufgaben.md`). **Lückenprüfung**
  (`einvoice_gaps`): Fehlen Angaben, die die XRechnung verlangt (Leitweg-ID/Käuferreferenz, IBAN, PLZ/Ort, Kontaktname, Telefon und
  E-Mail der eigenen Firma, E-Mail des Kunden, ein Zahlungshinweis ohne „#“ – das Zeichen gehört in der XRechnung der Skonto-Angabe …), wird die UBL-Datei **nicht geschrieben** (ein abgelegtes XML eines Entwurfs wird
  entfernt – am gespeicherten Pfad und am Namensschema, nur Dateien mit der Endung `.xml`, nie das PDF), das PDF mit CII entsteht immer; das Ergebnis trägt den Klartext („XRechnung (XML) nicht erstellt – es fehlt: …“), die
  Rechnungsseite zeigt die Lücken bei Entwurf/abgeschlossen, und vor dem Versenden fragt sie nach. Die UBL-Datei entsteht nur bei
  Leitweg-ID/Käuferreferenz (`wants_xrechnung`; eine Bestellnummer allein löst sie nicht aus). Der Kontoname (BT-85) wird nicht
  geschrieben (den Kontoinhaber gibt es nicht als Momentaufnahme). `tools/make_einvoice_samples.py` bricht ab, wenn ein Beispiel Lücken
  hat – Prüfung und Mustang müssen übereinstimmen
- Versendete und stornierte Rechnungen (`db.invoice_lock.FROZEN_STATUSES`) werden von
  „PDF + E-Rechnung“ nicht neu erzeugt (`overwrite_sent` nur beim Eintritt aus Entwurf/abgeschlossen in
  „versendet“); liegt für sie noch keine Datei vor (Import, gescheiterte Erzeugung), darf sie erzeugt werden
- **Erzeugt** wird nur an zwei Stellen: über „PDF + E-Rechnung“ im Formular und
  automatisch beim Wechsel von Entwurf/abgeschlossen auf Status **versendet** (ab da ist die Rechnung
  eingefroren). Eine bereits versendete Rechnung wird nicht überschrieben; versendet ↔ überfällig erzeugt nichts
- **Sperre versendeter Rechnungen** (`db/invoice_lock.py`, seit 2.1.2). Eine Stelle entscheidet
  (`check_changes`, Statusgruppen), und jeder Schreibweg auf eine Rechnung geht darüber: das Speichern
  (`Database.update_invoice_fields`, bei einer versendeten Rechnung nur aus `correct_invoice`) und der
  Statuswechsel (`Database.set_invoice_status`) – nie
  `update_invoice_status` (der ungeprüfte Baustein für Seed und Tests; Angebote: `set_quote_status`). Bearbeitbar ist alles bei
  `draft`/`finalized`. Bei `sent`/`overdue`/`partial_payment`/`partial`/`paid` sind Nummer, Rechnungsdatum,
  Kunde, eigene Firma, Positionen, Steuer, Summen, Fälligkeit, Skonto, Bank, Währung und Käufer-USt-IdNr.
  gesperrt: `/invoice/save` antwortet mit **409** und einem Klartext, nichts der Anfrage wird geschrieben.
  Änderbar bleiben der Zahlungshinweis (`PaymentTerms`, der Text unter den Positionen), die Anschrift,
  Bestellnummer, Leitweg-ID und Leistungsdatum – aber nur als **Berichtigung** (siehe dort). `cancelled` und
  die Stornorechnung (`row_group`: `InvoiceKind='storno'` gilt als abgeschlossen, gleich welchen Status sie trägt):
  gar nichts mehr. Geprüft wird gegen den gespeicherten Stand **in der Transaktion** (`BEGIN IMMEDIATE`, das
  `UPDATE` trägt die Statusgruppe im `WHERE`): zwei gleichzeitige Anfragen laufen nacheinander. Was das
  Formular unverändert zurückschickt, ist kein Verstoß (Beträge in Minor Units, Positionen Feld für Feld,
  Einheiten wie `unit_display` sie zeigt, Steuersatz 0,19 = 19). **Die Maske überschreibt bei einer
  versendeten Rechnung nichts still:** Sie zeigt die gespeicherten Werte (Anschrift der Rechnung, Leitweg-ID)
  und holt beim Laden nichts aus den Stammdaten; sie schickt nur die Felder, die der Nutzer geändert hat (kein
  `items`), und der Server schreibt nur, was vom Gespeicherten abweicht (`changed_open_fields`) – ein
  unverändertes Speichern ändert keine Spalte, auch nicht `NULL` zu leer (und legt weder Fassung noch
  Protokoll an). Die Anschrift ist eine
  Momentaufnahme und kommt nur auf ausdrücklichen Wunsch (`refreshAddress`, Kästchen „Anschrift aus den
  Stammdaten übernehmen“, mit alt → neu, nur bei Abweichung) neu aus den Stammdaten **desselben** Kunden; Absender,
  Bank und Käufer-USt-IdNr. sind abgeleitet und werden bei Sperre nie neu geholt. **Status und `AmountDue` kommen nie vom Client:** eine
  neue Rechnung ist `draft` (oder `finalized`), `AmountDue` ist im Entwurf das Brutto, danach rechnet es die
  Zahlungslogik. Ein unbekannter Status gilt als gesperrt
- **Berichtigen** (`Database.correct_invoice`, `db/invoice_versions.py`, seit 2.2.0): Das Speichern der änderbaren
  Felder einer versendeten Rechnung ist eine Berichtigung – kein eigener Schreibweg, es bleibt bei `/invoice/save`
  mit dem Teil-Payload (Knopf „Berichtigen“, nach Rückfrage). Gesperrte Felder werden zuerst abgelehnt; ohne Abweichung
  vom Gespeicherten passiert nichts – auch ohne `documentVersion`. **Das Leistungsdatum bleibt ein Datum:** Steht es
  unter den geänderten Angaben, muss es ein ISO-Datum sein (`CorrectionInvalid`, 400, vor dem Verschieben der
  Dokumente; die Maske schickt ein geleertes gar nicht erst ab) – ein unverändertes, auch leeres (Import), ist kein
  Verstoß.
  Sonst in EINER Transaktion (`BEGIN IMMEDIATE`, die Rechnung wird neu gelesen): Die Fassung, die die Maske
  geladen hat (`documentVersion`; `Invoices.DocumentVersion`, NULL = 1), muss die aktuelle sein, sonst 409 (fehlt
  sie oder ist sie veraltet, wird nichts geschrieben); PDF und XML wandern nach `Fassungen/<ID>/`
  (`archive_sent_documents`) und stehen mit Fassung, Pfad (relativ zu `invoices/`) und SHA-256 in
  `InvoiceDocumentVersions`, ihre Pfade in der Rechnung werden geleert; jedes geänderte Feld ist eine Zeile in
  `InvoiceCorrections` (`Action='correct'`: wer, wann, alt, neu, neue Fassung – bei `refreshAddress` je
  Anschriftsspalte eine); `DocumentVersion` zählt um eins hoch. Scheitert das Verschieben, das Festhalten in
  `InvoiceDocumentVersions` (`_archive_documents`) oder der Commit, liegen die Dateien wieder am alten Platz (409,
  nichts geändert). Danach erzeugt `handlers._correct_invoice` das Dokument
  der neuen Fassung – nur, wenn eines abgelegt war (ein Import bekommt Felder und Protokoll, kein Dokument).
  Scheitert das Erzeugen, bleibt die Berichtigung gespeichert, die Antwort trägt eine Warnung, und „PDF +
  E-Rechnung“ erzeugt die fehlende Datei (die Regel „eingefroren, aber keine Datei vorhanden“). **Die berichtigte
  Fassung trägt einen Hinweis** (PDF: Zeile vor der Positionstabelle und „Fassung: n“ im Kopf; CII BT-22; UBL
  `cbc:Note`; TypeCode 380 und Nummer bleiben): Er ergibt sich aus dem Stand der Datenbank
  (`correction_notice`: `DocumentVersion` > 1 und Berichtigungen zur aktuellen Fassung, keine Rückkehr auf
  Entwurf danach), nicht aus einem Parameter – auch ein später über „PDF + E-Rechnung“ erzeugtes Dokument trägt
  ihn, eine nach dem Zurückholen neu versendete Rechnung nicht. „Ersetzt die Fassung vom …“ nennt den jüngsten
  Protokolleintrag vor der ersten Berichtigung der aktuellen Fassung, der eine Berichtigung oder ein Versand
  (Entwurf/abgeschlossen → versendet) ist, sonst `SentAt`, sonst entfällt der Teil. Zeitpunkte stehen in UTC,
  Dokumente und Oberfläche nennen die Ortszeit. Die stornierte Rechnung bleibt ganz gesperrt. Eine Ersatzrechnung
  (*Stornobeleg*) trägt daneben ihren eigenen Hinweis „Ersetzt die stornierte Rechnung …“: Trifft beides zu, stehen beide
  Hinweise, der der Ersetzung zuerst
- **Statusübergänge** (`db.invoice_lock.TRANSITIONS`; die Statusauswahl bietet nur die erlaubten Ziele,
  `set_invoice_status` lehnt den Rest mit Klartext ab – 409, „bezahlt“ ohne Zahlung 400): Entwurf ↔
  abgeschlossen frei. **Entwurf/abgeschlossen → versendet** („Versenden“) ist der einzige Übergang, der die
  Forderung bucht und das Dokument erzeugt. Versendet ↔ überfällig bucht und erzeugt nichts (auch bei einem
  Import ohne Forderung der App). Teilzahlung/bezahlt und der Rückweg setzt die Zahlungslogik
  (`next_payment_status`); „bezahlt“ von Hand nur mit mindestens einer Zahlung (Skonto), der Rückweg von
  bezahlt/Teilzahlung nach versendet/überfällig von Hand nur ohne Zahlung (verwaister Zustand).
  Versendet/überfällig → Entwurf/abgeschlossen („zurückholen“) nur ohne Zahlung; die Forderung wird entfernt
  und die versendeten Dokumente wandern nach „Fassungen“ (siehe dort).
  **Nach „storniert“ geht der Status nur für eine Rechnung ohne positiven Betrag** (Brutto ≤ 0: selbst angelegte Gutschrift,
  0-€-Rechnung – sie bekommt keine Stornorechnung), mit dem bisherigen Buchungsverhalten (Art `cancel`: die Forderung geht,
  solange keine Zahlung daran hängt und nur die der App). Das entscheidet `check_transition` am Betrag, den
  `set_invoice_status` in der Transaktion liest (Bedingung `NO_POSITIVE` in `TRANSITIONS`; `check_transition` und
  `allowed_targets` nehmen ihn als `gross`, ohne Angabe gilt er als positiv). Für Brutto > 0 lehnt
  `set_invoice_status(…, 'cancelled')` mit 409 ab: Storniert wird über „Stornieren“ (Absatz *Stornobeleg*); die Seite zeigt den
  Knopf nur bei Brutto > 0 und „Storniert“ in der Statusauswahl nur bei Brutto ≤ 0. Storniert → versendet/
  überfällig („wiederbeleben“) gibt es nur für eine stornierte Rechnung **ohne** Stornorechnung (Altbestand,
  Import, von Hand storniert): bucht die Forderung neu und lässt ein vorhandenes Dokument stehen; mit Stornorechnung 409 – sie bleibt
  storniert. Die Stornorechnung selbst ändert ihren Status nie (409).
  **Status, Forderungsbuchung und (beim Zurückholen) das Ablegen der Dokumente laufen in EINER Transaktion**
  (`set_invoice_status`, `BEGIN IMMEDIATE` von der Prüfung bis zum Commit): Sie gelingen gemeinsam oder gar nicht;
  nur das Erzeugen des Dokuments beim Versenden folgt danach (`handle_update_invoice_status`). **Jeder gelungene
  Wechsel steht im Protokoll** (`InvoiceCorrections`, `Action='status'`, in derselben Transaktion; die
  Zahlungslogik `_recalc_invoice` protokolliert nicht), und `send` setzt `SentAt` beim ersten Versand (Importe
  lassen es NULL). Konten, die
  das Buchen anlegen könnte, werden vorher in eigenen Verbindungen aufgelöst (`_receivable_accounts`), nie
  innerhalb. **Die App entfernt nur die Forderung, die sie selbst gebucht hat** (`_receivable_removal`: ohne
  `SourceGroup`, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet) – beim Zurückholen, beim Storno von Hand
  (Brutto ≤ 0) und beim Entfernen der letzten Zahlung einer von Hand stornierten Rechnung (`_drop_receivable`): nie ein
  Satz mit Quellklammer. Hängt an der Rechnung eine importierte, von Hand gebuchte oder an eine Bankbewegung gehängte
  Forderung oder ein Zahlungssatz, lehnt das Zurückholen mit Klartext ab (409, nichts geändert); Stornieren, Storno von
  Hand und Zahlungsentfernen lassen sie stehen. **Beim Versenden einer Ersatzrechnung** (`ReplacesInvoiceId`, Art `send`)
  **ziehen die Zahlungen der stornierten Rechnung auf sie um** – in derselben Transaktion, nach der Forderungsbuchung
  (`_payment_move_context` löst das Erlöskonto vorher in eigenen Verbindungen auf, `_move_replaced_payments` schreibt;
  `update_invoice_status`, der ungeprüfte Baustein für Seed und Tests, zieht nie um). Voraussetzung: gleicher Kunde und
  gleiche Währung und ein hinterlegtes Erlöskonto für den Steuersatz der neuen Rechnung – sonst bleibt alles, und der
  Hinweis sagt es. **Nach einer Erstattung zieht höchstens das verbleibende Guthaben um:** `movable_left` = alle Zuordnungen der
  stornierten Rechnung (auch eine negative des WISO-Imports: Sie zieht nie um, mindert aber das Guthaben) minus die
  Erstattungen an der Stornorechnung, vor dem Umzug; je umziehbarer Zuordnung ist der Teil `min(Betrag, Rest der neuen
  Rechnung, movable_left)`, bei `<= 0` endet der Umzug, nach einem gelungenen Umzug sinkt `movable_left` um den Teil, der Rest
  der Zuordnung bleibt wie sonst an der stornierten Rechnung (`allow_cancelled`). So ist danach nie mehr erstattet als gezahlt
  (50,00 € gezahlt, 20,00 € erstattet, Ersatzrechnung über 119,00 €: 30,00 € ziehen um, 20,00 € bleiben, Guthaben 0); ohne
  Erstattung bremst `movable_left` nie. Der Hinweis nennt das Erstattete („… sind bereits erstattet und bleiben an der
  stornierten Rechnung.“); „bleiben als Guthaben des Kunden“ nennt nur das verbleibende Guthaben, ohne den erstatteten Teil.
  Umgezogen wird je Zuordnung (älteste zuerst, solange die neue Rechnung offen ist), und nur, was die
  App **genau so geschrieben hat** und sich eindeutig neu schreiben lässt (`_movable_payment`: Bankbewegung mit
  Zahlungseingang und SKR-Konto, nicht überbucht, ohne Sätze mit `SourceGroup`, genau eine Zuordnung zum Paar).
  **Die Sätze der Zuordnung müssen genau die sein, die `_assign_in_tx` dafür schreibt** (`_revenue_coa_if_as_written`,
  Vergleich mit `payment_rows` – derselben Stelle, aus der das Schreiben seine Werte nimmt): der Bestand („Bank an
  Debitoren“ und „Wartekonto an Erlöskonto“ oder der Einzelsatz „Erlöskonto an Bank“, keiner mehr, keiner weniger), das
  Bankkonto der Bewegung, die übrigen Konten in ihrer Rolle, Betrag, Datum, Steuerdatum, Empfänger, Währung, Kunde, Text,
  Steuersatz, der Steuerbetrag (anteilig – oder der Rest, aber nur, wenn die Rechnung voll bezahlt war: Nur der schließende
  Teil bekommt ihn; `_prorated_tax`, `_remaining_tax`), Debitoren- und Wartekonto die der Forderung (`_receivable_sides`,
  dieselbe Stelle, aus der `_assign_in_tx` sie nimmt), keine Kategorie; kein Beleg und kein anderer Verweis an den Sätzen, den `_delete_booking_rows` wegräumt
  (`_booking_attachments_exist`, `BOOKING_REFERENCES`), unter der Bewegung kein markierter Satz mit einer Beleg-Nr.,
  die zu keiner ihrer Zuordnungen gehört, und kein Satz ohne Marke, der die Beleg-Nr. der Rechnung trägt oder die Debitoren
  berührt (ein von Hand ergänztes Skonto „… an 10000“ bliebe sonst zurück, und die neue Rechnung zeigte einen offenen Rest).
  Bei gleichem Steuersatz muss außerdem das Erlöskonto der Sätze das heute hinterlegte sein – der Umzug bucht auf dieses und
  verschöbe den Erlös sonst im vergangenen Zeitraum auf ein anderes Konto. Sonst bleibt die Zuordnung liegen – der Umzug löschte die Sätze und schriebe sie
  neu, eine von Hand korrigierte Steuer, ein geändertes Datum oder ein verknüpfter Beleg gingen verloren, ein markierter
  Satz mit geänderter Beleg-Nr. bliebe als Doppelbuchung stehen. Beurteilt wird jede Zuordnung gegen den Stand VOR dem
  ersten Umzug (der Steuer-Rest des schließenden Teils hängt von den anderen ab). Importe (Zuordnung ohne Sätze),
  überbuchte Bewegungen und geänderte Sätze bleiben an der stornierten Rechnung; eine Sammelzahlung zieht nur mit dem Anteil
  der stornierten Rechnung um, die Zuordnungen anderer Rechnungen bleiben. Je Zuordnung wird sie samt Sätzen entfernt
  (`_unassign`), die neue Rechnung
  bekommt den Betrag, den sie noch braucht (`_assign_in_tx`: Datum der Bankbewegung, Steuersatz und Erlöskonto der neuen
  Rechnung; Steuer anteilig, der schließende Teil den Rest), und ein Rest wird an der stornierten Rechnung wieder
  zugeordnet (`allow_cancelled`, auf dem bisherigen Erlöskonto): Er ist ihr Guthaben. Weicht der Steuersatz ab, ändert sich
  die Umsatzsteuer am Zahlungstag; bei gleichem Satz und Betrag bleibt die EÜR unverändert. **Je Zuordnung läuft das in
  einem Savepoint:** Lässt sie sich nicht vollständig mit den erwarteten Beträgen neu buchen, geht er zurück (Zuordnung und
  Sätze unverändert, ihre Einträge im Replay-Log entfallen), sie zählt als übersprungen, der Versand gelingt, die nächste
  Zuordnung kommt an die Reihe; nur ein unerwarteter Datenbankfehler lehnt den Versand ab (409, `PaymentMoveFailed` →
  `_Refusal`) und nichts ist geändert. **Was geschah, nennt `hint` in der Antwort** (`StatusChange.payment_hint`) – und
  bei dem, was bleibt, den wirklichen Grund (`MOVE_SKIP_REASONS`: Import, Bankkonto ohne SKR-Nummer, überbuchte Bewegung,
  Sätze ohne Marke (ältere Version, von Hand angelegt), geänderte oder ergänzte Sätze oder Beleg, ein anderes Erlöskonto
  als das hinterlegte, allein ein anderes Steuerdatum – so stempelt der DATEV-Export die exportierten Sätze, sie
  ziehen deshalb nicht um –, mehrfach zugeordnete Bewegung, keine Bankbewegung mit Zahlungseingang, nicht neu buchbar), je
  Grund einmal in fester Reihenfolge, mit dem Betrag des Übersprungenen insgesamt. Entfernt der Nutzer die Zahlung an der
  neuen Rechnung später von Hand, wandert nichts zurück: Der Betrag ist freier Rest der Bewegung (Guthaben in den offenen
  Posten) und lässt sich jeder offenen Rechnung zuordnen
- **Stornobeleg** (`Database.cancel_invoice`, `db/invoice_storno.py`, seit 2.2.1; `POST /invoice/cancel`, JSON
  `{"invoiceId", "date"}` – `invoiceId` streng als Ganzzahl wie bei `/invoice/save` –, Route in `_JSON_PATHS` und
  `_POST_ONLY_JSON_PATHS`: CSRF-Token und Herkunft wie `/invoice/status`, GET → 405): Eine versendete Rechnung
  (versendet/überfällig/Teilzahlung/bezahlt) wird über den Knopf „Stornieren …“ storniert, mit Rückfrage und Stornodatum
  (vorbelegt mit heute, nicht vor dem Rechnungsdatum und nicht nach heute; `appConfirm` hat dafür ein optionales Datumsfeld
  mit `min`/`max`) – der Knopf erscheint nur bei Brutto > 0, eine Rechnung ohne positiven Betrag wird über den Status
  storniert (*Statusübergänge*). Es entsteht ein
  eigener Beleg, die **Stornorechnung**: Nummer aus dem Rechnungskreis des **Jahres des Stornodatums**
  (`allocate_invoice_number`), Kopf der Ursprungsrechnung (Anschrift-Momentaufnahme, Leistungsdatum, Bank, Steuer, Leitweg-ID …)
  ohne Fälligkeit und Skonto, `PaymentTerms` = „Der Betrag wird verrechnet bzw. erstattet.“ (`STORNO_PAYMENT_TEXT`),
  `InvoiceKind='storno'`, `CancelsInvoiceId`, Status `sent` mit `SentAt`, **Summen exakt negiert** (Minor Units mal −1,
  nichts neu gerechnet), Positionen mit negativer Menge und positivem Einzelpreis, `AmountDue` = ihr (negatives) Brutto.
  Die Ursprungsrechnung wird `cancelled`. Alles in EINER Transaktion (`BEGIN IMMEDIATE`: Prüfung, Nummer, Beleg, Positionen,
  Status, je eine Protokollzeile für beide Belege, Gegenbuchung, Neuberechnung) – ganz oder gar nicht, nach einem Fehler ist
  keine Nummer verbraucht; die Konten der Forderung löst sie vorher in eigenen Verbindungen auf, aber erst, wenn die Rechnung
  sich stornieren lässt. Abgelehnt wird mit Klartext und Statuscode, nichts geschrieben: Stornodatum kein ISO-Datum, vor
  dem Rechnungsdatum oder **in der Zukunft** (400; maßgeblich ist `_today()`, das Ortsdatum des Servers – sonst legte ein
  Tippfehler wie 2062 einen Nummernkreis 2062 an; nur die Demodaten übergeben ihr Szenariodatum als `today`), unbekannte ID oder
  Angebot (404), Stornorechnung, Entwurf/abgeschlossen, schon mit Stornorechnung storniert, unbekannter Status, Brutto ≤ 0,
  schon eine Stornorechnung (409; der eindeutige Index auf `CancelsInvoiceId` fängt es zusätzlich ab); scheitert das Speichern an einem
  Verweis ins Leere (`ForeignKeyError`, etwa ein gelöschtes Bankkonto), nennt die Meldung ihn im Klartext (409, nichts
  „repariert“). **Die Forderung wird nie gelöscht:** Hat die App sie gebucht (`_is_app_receivable`, dieselbe Einordnung
  wie in `_receivable_removal`, nur dass es nicht zählt, ob sie an einer Bankbewegung hängt), bucht die Stornierung die
  **Gegenbuchung** („Debitoren an Wartekonto“ über `_book_receivable`, Form `transfer`, negativer Betrag und negative Steuer,
  Datum und Beleg-Nr. der Stornorechnung – für `receivable_condition` die Forderung einer Gutschrift); sonst (Import,
  Steuersatz ohne Wartekonto, importierte oder von Hand gebuchte Forderung) keine, und die Antwort trägt einen `hint`. Zahlungen
  bleiben an der stornierten Rechnung (der Umzug auf die Ersatzrechnung: *Statusübergänge*; die Erstattung: unten).
  Ihr Betrag ist das **Guthaben des Kunden: Σ `InvoicePayments.Amount` über beide Belege des Paares** (Rechnung und
  Stornorechnung; `get_storno_credits`, `get_storno_credit`, `get_storno_credits_for_contact` – rein lesend, ein Statuswechsel
  der Stornorechnung gehört nicht dazu). Es steht in der Rechnungsliste an der Stornorechnung („Erstattung offen: …“ statt
  „Stornorechnung“; nach voller Erstattung wieder „Stornorechnung“), als Zeile auf den Seiten beider Belege (ganz erstattet:
  „Guthaben vollständig erstattet.“) und als Abschnitt „Guthaben aus stornierten Rechnungen“ bei den
  offenen Posten des Kontakts (`storno_credits`; der Saldo zieht es ab, denn 10000 führt es im Haben). Alle zeigen das
  verbleibende Guthaben.
  **Erstattung** (`Database.assign_storno_refund`, `db/invoice_payments.py`, seit 2.2.3; Weg: `POST /invoice/link-payment` mit
  der ID der Stornorechnung, `handle_link_invoice_payment` erkennt sie – keine neue Route, Prüfungen unverändert): Eine
  ausgehende Bankbewegung (Betrag < 0, Bankkonto mit SKR-Nummer) gleicht das Guthaben aus, zugeordnet **an der Stornorechnung**
  mit negativem Betrag; erstattet wird min(Guthaben, freier Rest der Bewegung, gewünschter Betrag auf Cent gerundet). Die Sätze
  schreibt `payment_rows` (wie bei der Zahlung) mit Datum, Steuerdatum, Empfänger und Währung der Bewegung, der **Nummer der
  Stornorechnung** als Beleg-Nr. und der Marke `invoice_payment`, alle negativ: Hat die Stornorechnung eine Gegenbuchung
  (`_receivable_sides`), „Bank an Debitoren“ und „Wartekonto an Erlöskonto“, sonst der Einzelsatz „Erlöskonto an Bank“.
  **Steuer:** Sie nimmt zurück, was die Zahlungen an der stornierten Rechnung gebucht haben (`_allocations_tax`); gleicht die
  Erstattung das Guthaben ganz aus, ist es der Rest (Steuer der Zahlungen minus die früherer Erstattungen), sonst anteilig (Cent,
  halb auf, nie mehr als der Rest) – die Summe aller Erstattungen ist die Steuer der Zahlungen, **solange sich die Zahlungen der
  stornierten Rechnung nach der ersten Erstattung nicht ändern** (wird danach eine Zahlung entfernt oder zieht ein Teil auf die
  Ersatzrechnung um, kann die Summe um Cent abweichen). **Erlöskonto:** das der Zahlungen (`_payment_revenue_coa` über die
  markierten Sätze ihrer Zuordnungen), wenn alle dasselbe tragen; tragen sie **mehr als ein** Erlöskonto (ein Umbuchungssatz
  wurde im Editor geändert, `refund_revenue_coas_differ`), wählt der Nutzer das der Erstattung (`revenue_coa_id`, ein
  Erlöskonto, sonst Ablehnung „Die Zahlungen dieser Rechnung liegen auf verschiedenen Erlöskonten – bitte das Erlöskonto für die
  Erstattung wählen.“; kein hinterlegtes Konto entscheidet das, und es legt keines an). Ohne markierte Sätze (Import) das für den
  Steuersatz hinterlegte, sonst wählt der Nutzer. Der Kasten „Erstattung“ zeigt die Auswahl in beiden Fällen (mit dem Satz oben
  als Hinweis); „Die Wahl gilt künftig als Vorgabe für diesen Steuersatz.“ steht dort nicht – Erstattungssätze lehren kein
  Erlöskonto, bei der Zahlung bleibt der Satz. Vorprüfung vor der Transaktion (eine abgelehnte Erstattung legt kein Konto an), Prüfung
  frisch gelesen in ihr; danach `_recalc_invoice` (`AmountDue` = Brutto − Zuordnungen, der Status der Stornorechnung ändert sich
  nie). Die Bankzeile bekommt nur den Kunden nachgetragen (die Route, nicht die DB-Schicht) – sonst füllte ihn die Bankmaske beim
  ersten Speichern ein. **Entfernen** wie jede Zuordnung: Forderung und Gegenbuchung bleiben, das Guthaben ist wieder da. **Das
  Guthaben wird auf keinem Weg der Oberfläche negativ:** Jeder Weg, der eine **Zahlung** an einer stornierten Rechnung mit
  Stornorechnung entfernt, fragt vor dem ersten Schreiben in seiner Transaktion dieselbe Prüfung (`_refund_removal_blocker`:
  bliebe von den Zuordnungen beider Belege weniger als 0, lehnt `PaymentRemovalRefused` ab, nichts ist geändert): die
  Rechnungsseite (`unassign_invoice_payment`, 409), das ✕ an einer Zeile der Zahlung im Bank-Editor und das Löschen eines
  Satzes der Zuordnung (`remove_assignment_for_child`), `delete_transaction` – Satz wie Bankbewegung – und die Liste
  (`/transactions/delete`). `_unassign` selbst prüft nichts (der Umzug braucht es so). Der Editor prüft **alle** entfernten
  Zeilen zusammen schon beim Planen (`_plan_split_rows` → `Database.split_removal_blocker`, rein lesend, ein nur abgehängter Satz
  zählt nicht): abgelehnt wird vor dem ersten Schreiben (400 mit Klartext, nichts gespeichert). Seit 2.3.15 läuft das ganze
  Speichern des Editors in EINER Transaktion (`save_bank_with_rows`, siehe *Masken*): Geplant wird unter der Schreibsperre, die
  Einzelprüfung beim Entfernen bleibt zusätzlich – lehnt sie ab oder scheitert eine Zeile, ist nichts gespeichert; die Löschroute
  leitet mit dem Grund auf die Liste (`/transactions?err=`, `appMsg`). Das Entfernen einer **Erstattung** bleibt auf allen Wegen
  frei. Die Prüfliste meldet `refund_exceeds_credit` weiter für Eingriffe an der App vorbei. Nach einer Erstattung zieht höchstens das
  verbleibende Guthaben auf die Ersatzrechnung um (*Statusübergänge*). Die Seite der Stornorechnung zeigt den Kasten „Erstattung“ (ausgehende Bewegungen mit
  freiem Rest, Betrag vorbelegt; die Erstattungen mit „Entfernen“); die EÜR nimmt den Erlös am Tag der Erstattung zurück, das
  Dashboard zählt die Bankbewegung als Ausgabe.
  **Der Stornorechnung lässt sich keine Zahlung zuordnen** (`payment_blocker`, `assign_invoice_payment`,
  `link_invoice_to_transaction`; sie ist nie Kandidat, nie offener oder überfälliger Posten – ihre Zuordnung ist allein die
  Erstattung), und die Zahlungslogik ändert ihren Status nie. **Die Stornorechnung ist ganz gesperrt:** Status (409), Speichern, Berichtigen, Löschen, Kopieren – jede
  Abweichung wird mit „Stornorechnung: Sie lässt sich nicht ändern.“ abgelehnt, unverändertes Speichern ändert nichts. Ihre
  Seite ist schreibgeschützt und nennt die Ursprungsrechnung (Link); die Seite der stornierten Rechnung nennt die
  Stornorechnung. In der Rechnungsliste trägt die Stornorechnung statt des Status „Stornorechnung“, zeigt negative Beträge
  und zählt in keiner Summe der Statistik mit. **Dokument:** Typ 381 – CII mit `ram:InvoiceReferencedDocument` (BT-25/BT-26)
  und BT-22 „Storno zu Rechnung … vom …“, UBL als `CreditNote` mit `cac:BillingReference`, `cbc:CreditedQuantity` und ohne
  `cbc:DueDate` (derselbe Code wie die Rechnung, `export/xrechnung_invoice.py`); im XML stehen die Beträge **positiv** (das
  Modell negiert genau einmal), das PDF zeigt die gespeicherten negativen Beträge mit dem Titel „Stornorechnung“ und „zu
  Rechnung Nr. … vom …“; sie trägt nie den Berichtigungs-Hinweis. Das Dokument der Ursprungsrechnung bleibt unangetastet.
  Erzeugt wird es nach dem Commit (`handle_invoice_cancel`; ein Fehler dabei lässt die Stornierung stehen, die Antwort trägt
  eine `warning`, „PDF + E-Rechnung“ erzeugt die Datei nach). Der E-Mail-Versand nennt sie „Stornorechnung“ und fordert nichts
  an.
  **Neu ausstellen** (`Database.create_replacement_draft`, `db/invoice_storno.py`, seit 2.2.2; `POST /invoice/reissue`, JSON
  `{"invoiceId"}`, Route in `_JSON_PATHS` und `_POST_ONLY_JSON_PATHS` wie `/invoice/cancel`; der Knopf „Stornieren und
  neu ausstellen …“ schickt `reissue: true` an `/invoice/cancel` – ein Wert, der kein Wahrheitswert ist, ist eine
  ungültige Anfrage, bevor etwas geschieht): Zu einer stornierten Rechnung **mit** Stornorechnung legt die App den Entwurf
  einer neuen Rechnung an – Kopf und Positionen der stornierten (wie bei der Stornorechnung, aber positiv, nichts neu
  gerechnet; Leistungsdatum, Zahlungshinweis und Skonto übernommen), Nummer aus dem Kreis des Jahres von heute (`_today()`),
  heutiges Datum, Fälligkeit um dasselbe Zahlungsziel verschoben (sonst leer), Status `draft`, `Invoices.ReplacesInvoiceId`
  = die stornierte Rechnung; ein Entwurf bekommt, wie jeder, keine Protokollzeile. **Höchstens eine Ersatzrechnung je
  stornierter Rechnung** (Prüfung in der `BEGIN IMMEDIATE`-Transaktion, einen eindeutigen Index gibt es nicht; nach dem
  Löschen des Entwurfs geht es wieder, und seine Nummer ist frei, solange er nie versendet war). Abgelehnt wird mit Klartext,
  nichts geschrieben, kein Zähler verbraucht: unbekannte ID oder Angebot (404); Stornorechnung, nicht stornierte Rechnung,
  stornierte ohne Stornorechnung, schon ersetzte Rechnung, Nummernkreis ohne brauchbare Nummer, Fehler beim Speichern –
  ein Verweis ins Leere steht im Klartext (409). Bei „Stornieren und neu ausstellen“ ist der Entwurf eine zweite
  Transaktion nach Stornierung und Dokument (`handle_invoice_cancel`): Scheitert er, bleibt die Stornierung stehen, und die
  Antwort trägt eine `warning` („Neu ausstellen“ an der stornierten Rechnung holt es nach). „Als neu kopieren“
  (`copy_from`) setzt nie `ReplacesInvoiceId`. **Dokument:** Die Ersatzrechnung trägt „Ersetzt die stornierte Rechnung …
  (Stornorechnung …)“ (PDF: fette Zeile vor der Positionstabelle; CII BT-22; UBL `cbc:Note`; TypeCode 380) – aus dem Stand
  der Datenbank (`replacement_note`), nicht aus einem Parameter; ihre Seite nennt die stornierte Rechnung, die stornierte
  nennt die neue.
  **Nachträglich stornieren:** Eine von Hand stornierte Rechnung ohne Stornorechnung (Altbestand) und mit Brutto > 0 bekommt
  sie über „Stornorechnung nachträglich erstellen …“ (`cancel_invoice` auf eine Rechnung im Status `cancelled`: die
  Rechnung bleibt mit Status und Protokoll, wie sie ist; die Gegenbuchung entsteht nur, wenn ihre Forderung der App noch
  in den Büchern steht, sonst trägt die Antwort den `hint`; die Prüfliste meldet solche Fälle als `legacy_cancelled_open`).
  Danach gilt für sie, was für jede Rechnung mit Stornorechnung gilt (kein Wiederbeleben)
- **Rechnungsnummer beim ersten Speichern** (`allocate_invoice_number`, `release_invoice_number`,
  `peek_next_number` in `db/numbering.py`): Die Nummer wird in der Transaktion des Speicherns vergeben – auch
  als Entwurf, bei Angebot → Rechnung ebenso –, der Zähler rückt dort vor (`UPDATE` + `SELECT` in der
  `BEGIN IMMEDIATE`-Transaktion, kein `RETURNING`: ältere SQLite-Versionen kennen es nicht); scheitert das
  Speichern, ist sie nicht verbraucht. Der Vorschlag der Maske ist genau diese Nummer (Formatvorlage und Zusatz
  des Kreises, eine belegte Nummer wird übersprungen). **Die Nummer im Feld zählt nur als Vorschlag, solange
  sie unverändert bleibt:** Die Maske schickt ihn mit (`numberSuggestion`); ist die Nummer beim Speichern genau
  dieser Vorschlag, vergibt der Server frisch – ein zweites Fenster kann ihn verbraucht haben – und nennt die
  vergebene Nummer in der Antwort (`invoice_number`), die Maske zeigt sie. **Vergeben wird nach dem Jahr des
  Rechnungsdatums:** Gibt es für das Jahr keinen Kreis, entsteht er (Buchstabe, Zusatz, Vorlage des jüngsten).
  Eine eigene Nummer, die zur Vorlage eines Kreises passt, hebt dessen Zähler, nie nach unten. Statuswechsel und
  spätere Änderungen zählen nie. **War die Rechnung schon einmal versendet** (`Database.was_sent_before`), ist die
  Nummer auch im Entwurf gesperrt (409 „Die Rechnung war bereits versendet – ihre Nummer bleibt.“; die Maske zeigt
  das Feld `readonly` mit Hinweis; eine Nummer, die sich nur durch Leerraum unterscheidet, bleibt unberührt und wird
  nicht geschrieben). `invoiceId` liest `/invoice/save` als Ganzzahl (`"1"` und `1.0` gelten, alles andere – `"01"`,
  `1.5`, `true` – ist 400): Der Ordner `Fassungen/<ID>` heißt nach der Zahl. Eine Formatvorlage braucht **genau einen Zähler** (`{n}`, `{nn}` oder `{nnn}`,
  `number_format_error`): Beim Speichern eines Nummernkreises wird sie sonst mit Klartext abgelehnt; ein
  Altbestand mit anderer Vorlage lässt den Vorschlag leer (Hinweis in der Maske, `peek_next_number` wirft nie)
  und das Speichern mit dem Klartext `NumberRangeError` scheitern, nie die Seite
- **Entwurf löschen** (`POST /invoice/delete?id=`, `Database.delete_draft_invoice`): nur Status `draft`, ohne
  zugeordnete Zahlung und ohne Forderungsbuchung, in einer Transaktion; eine Ablehnung geht mit dem Grund
  (`err`) zurück auf die Rechnung. Ein Quellangebot im Status `converted` ohne weitere Rechnung geht auf
  `accepted` zurück. PDF und XML des Entwurfs räumt `handlers.handle_delete_invoice` danach weg – nur im
  Dokumentordner, nur wenn kein anderer Beleg darauf verweist und nie etwas in „Fassungen“
  (`remove_document_file`). Ein abgeschlossener Entwurf wird erst auf Entwurf
  zurückgesetzt. **Angebot → Rechnung** (`convert_quote_to_invoice`) geht einmal je Angebot und nur aus
  Entwurf/versendet/angenommen (`UPDATE … WHERE Status IN (…)` in einer Transaktion; Klartext bei der Ablehnung).
  Ein Angebot **bleibt „umgewandelt“, solange es eine Rechnung daraus gibt**: `/quote/status`
  (`set_quote_status`) und `/quote/save` (`QUOTE_STAYS_CONVERTED_SQL` im `UPDATE`) lassen den Status nicht
  verlassen – sonst ließe es sich auf „angenommen“ setzen und ein zweites Mal umwandeln
- **Fassungen (`export/invoice_files.py`):** Geht eine versendete Rechnung zurück auf Entwurf/abgeschlossen oder wird
  sie berichtigt, **verschiebt** `archive_sent_documents` PDF und XML nach
  `invoices/<Jahr>/Fassungen/<ID>/<bisheriger Name> – versendet bis <JJJJ-MM-TT>.<pdf|xml>` – nie überschreiben
  (ein vergebener Name bekommt einen Zähler), nie löschen, nur Dateien im Dokumentordner, die kein anderer Beleg
  führt (ein umgebogener Pfad außerhalb bleibt, wo er ist; was schon in einem Ordner „Fassungen“ liegt,
  wird nie weiterverschoben; zeigt der gespeicherte Pfad woandershin, wird zusätzlich die Datei am
  Namensschema abgelegt), `PDFPath`/`XMLPath` werden geleert. Beim Zurückholen zählt `DocumentVersion` um
  eins hoch – nur, wenn eine Datei abgelegt wurde –, und `InvoiceDocumentVersions` hält Fassung, Pfad und
  SHA-256 je Datei fest (Anlass `reopened`, beim Berichtigen `corrected`). Scheitert ein
  Verschieben, geht das Verschobene zurück und das Zurückholen wird abgelehnt (auch ein scheiternder Commit legt
  es zurück). Erneutes Versenden erzeugt eine neue Datei am Namensschema; die Fassung bleibt. **„War schon
  versendet“** beantwortet `Database.was_sent_before` (auch in einer Transaktion): `SentAt` ist gesetzt, ODER
  das Protokoll kennt einen Statuswechsel aus einem versendeten Status, ODER – Bestand aus 2.1.2 – eine Datei
  liegt in `Fassungen/<ID>` in irgendeinem Jahresordner (`export.invoice_files.was_sent_before`):
  `delete_draft_invoice` gibt die Nummer dann **nie** frei (die Lücke bleibt; auch nicht bei einer versendeten
  Rechnung ohne abgelegte Datei), und die Rückfrage beim Löschen sagt es. Der Ordner liegt unter `invoices/` und
  ist damit Teil von Datensicherung und Wiederherstellung (`tests/test_invoice_versions.py` prüft es). Die
  Rechnungsseite zeigt die Fassungen und das Protokoll im Kasten „Fassungen und Protokoll“ (Tabellenzeilen und
  Dateien aus 2.1.2 ohne Zeile, die ohne Fassungsnummer erscheinen)
- **Dateinamen-Kollision:** Gehört der Zielpfad der abgelegten Datei eines anderen Belegs (zwei Nummern, die zum
  selben Namen werden), bekommt der Name die ID als Zusatz (`<Nr.> <Kunde> (ID n).pdf`,
  `invoice_file_path(…, db)`); der Rückfall über das Namensschema nimmt nie die Datei eines anderen Belegs.
  **Das Dokument zeigt nie den Stand der Zahlungen** (`prepaid` ist immer 0, Zahlbetrag = Brutto)
- **Heruntergeladen** wird immer die abgelegte Datei (`/invoice/pdf_download`,
  `/invoice/xml_download`) – nie eine frisch erzeugte. Fehlt sie, kommt 404 mit
  kurzem Klartext, den das Frontend direkt anzeigt. Die Fassungen liefert `GET /invoice/version_download?id=&file=&year=`,
  rein lesend: `file` ist ein reiner Dateiname (kein Trenner, kein `..`), gefunden wird nur eine `.pdf`-/`.xml`-Datei
  in einem Ordner `Fassungen/<id>` genau dieser Rechnung (`path_in_documents`); `year` (Jahresordner, nur ASCII-Ziffern;
  die Links des Kastens tragen es immer) trennt gleiche Dateinamen in zwei Jahresordnern – die Datei muss in genau diesem
  Ordner liegen, ohne `year` gilt der erste Treffer; alles andere – fremde ID, Angebot, Pfadanteil, ungültiges oder
  leeres `year` – ist 404
- **Angebote** gelten genauso: `/quote/pdf_download` liefert nur die abgelegte PDF
  (`quotes/<Jahr der Erzeugung>/<Angebots-Nr.> <Kunde>.pdf`), sonst 404 „Kein PDF
  vorhanden – bitte zuerst erzeugen.“. **Erzeugt** wird nur per POST
  (`/invoice/pdf_generate`, `/quote/pdf_generate`; GET → 405) – die Oberfläche
  erzeugt zuerst und lädt dann. Der Stundenzettel (`/worktime/pdf`, GET) schreibt
  nichts auf die Platte (`generate_worktime_pdf(store=False)`)
- **Belegtyp:** Rechnungen und Angebote teilen sich die Tabelle `Invoices`; jede Rechnungs- und
  jede Angebotsroute prüft den Typ (`Database.get_document`, `invoices.document_type`, beim
  Schreiben `… AND DocumentType = …`) – die ID eines Belegs der anderen Art ergibt 404 bzw.
  „Rechnung/Angebot nicht gefunden.“ und ändert, versendet und erzeugt nichts
- **Gespeicherte Dokumentpfade** (`Invoices.PDFPath`/`XMLPath`) gelten nur innerhalb des
  Dokumentordners des Benutzers (`<Datenordner>/invoices`, bei Angeboten `quotes`;
  `export/invoice_files.path_in_documents`, aufgelöst mit `realpath`): Die Datenbank lässt
  sich per SQL-Konsole oder zurückgespielter Sicherung ändern, sonst ließe sich darüber
  jede lesbare Datei ausliefern oder löschen. Jede Stelle, die einen gespeicherten Pfad
  öffnet oder löscht (Download, E-Mail-Anhang, `drop_stale_file`), geht darüber
  (`stored_invoice_path`). Ein Pfad außerhalb gilt als nicht vorhanden (Fallback über das
  Namensschema, sonst 404) und steht nur im Server-Log, nie in der Antwort
- Im Browser läuft der Download über `appDownload()` (`server/pages.py`): Datei
  als Blob holen und per `<a download>` speichern. Kein Tab mit Server-URI –
  der wäre ohne laufenden Server wertlos. Der Dateiname kommt prozentkodiert
  im Header `X-Filename` (Umlaute!). **Ausnahme Sicherungen** (*Sicherungen herunterladen*): ein gewöhnlicher Link auf
  `/backup/download` (die Antwort trägt `attachment`), ohne `appDownload()` – ein Blob läge bei Gigabyte-Größe ganz im Speicher
  des Browsers

### Testing
- Vor PR: lokal starten (`python main.py`), Feature testen
- Wenn Bug reproduzierbar, direkt über DB debuggen
- SQL-Queries gezielt checken (z.B. `SELECT COUNT(*) ... GROUP BY ...`)
- **Tests schreiben nie ins echte `data/`:** `tests/conftest.py` gibt jedem Test eine
  eigene Datenwurzel (`userctx.DATA_ROOT` unter `tmp_path`); ein Session-Wächter
  (`tests/data_guard.py`) schlägt an, wenn doch etwas dort landet
- **Tests hängen nicht an der Uhr des Imports:** Ein „heute“ steht nie in einer Modulkonstante, einer
  `parametrize`-Liste, einem Standardwert oder einer Fixture mit größerem Scope als `function`, sondern als
  Funktion, die im Test frisch liest (die Suite läuft 15–25 Minuten – ein Lauf über Mitternacht ließ den
  Release-Lauf von 2.3.4 scheitern). Wo die App einen Haken kennt (`invoice_storno._today`, `today=`), ein
  festes Datum nehmen; Erwartungen nie an ein festes Jahr oder eine Fälligkeit knüpfen, die die echte Uhr
  überholt. `tests/test_clock_at_import.py` prüft die Import-Regel statisch
- **Nach einem Merge mit Versionssprung den Release-Lauf nachsehen** (letztes Release bzw. der Lauf
  „Docker-Image veröffentlichen“): Scheitern dort die Tests, gibt es weder Image noch Release
- Alles rund um Testläufe liegt unter `data/tests/`: `tmp/` (Arbeitsdaten; nach dem
  Lauf bleiben nur die Verzeichnisse fehlgeschlagener Tests der letzten zwei Läufe)
  und `cache/` (pytest-Cache). Keine Punkt-Verzeichnisse im Projektordner anlegen.
  `data/tests/` wird von Datensicherung und Umzug (`layout.py`) übergangen, der
  Benutzername `tests` ist reserviert

### Versionierung
- Single Source of Truth: `version.py` (Schema `MAJOR.MINOR.PATCH`)
- **Bei jeder DB-Schema-Änderung** (`db/schema.py`) `SCHEMA_STEP` +1 → die mittlere
  Versionsstelle und `PRAGMA user_version` ziehen automatisch mit; PATCH dabei auf 0.
  Der neue Migrationsschritt prüft gegen die neue Zahl (`if from_version < 2001:`)
- In der Datenbank steht `SCHEMA_VERSION = MAJOR × 1000 + SCHEMA_STEP` (2.0.x → 2000,
  2.1.x → 2001, 2.2.x → 2002, 2.3.x → 2003, 2.4.x → 2004, 2.5.x → 2005, 2.6.x → 2006); Datenbanken aus 1.x tragen 0 bis 6. Eine Datenbank mit höherer Zahl
  als das Programm wird nicht geöffnet (`db.SchemaTooNewError`) – nie zurückstempeln
- Sonstige Änderungen ohne DB-Bezug: nur `PATCH` +1; große Umbrüche: `MAJOR` +1,
  `SCHEMA_STEP` und `PATCH` auf 0
- **Bei jedem Versionssprung `python tools/project_stats.py` ausführen und `project_stats.json` mit committen:** Die Datei trägt die Zahlen
  von „Projekt in Zahlen“ auf der About-Seite (Dateien, Zeilen und Zeilen ohne Leerzeilen und Kommentare für Programm, Tests und
  Beispieldaten, dazu Testfunktionen und Testfälle) und die Version, aus der sie stammen; `tests/test_project_stats.py` verlangt, dass
  sie zu `APP_VERSION` gehört (`python tools/project_stats.py --check` prüft nur das). Das Werkzeug zählt im Projektordner (`os.walk`, nur
  Standardbibliothek, ohne `git`; Auswahl und Ausschlüsse stehen als Daten oben im Skript), sammelt die Testfälle mit
  `pytest --collect-only` ein – scheitert das, schreibt es nichts – und schreibt weder Datum noch Pfade: Die Datei ändert sich nur, wenn sich
  das Projekt ändert. Docstrings zählen als Kommentar. Die About-Seite liest nur diese Datei (`server/project_stats.py`, streng: höchstens
  64 KB, ganze Zahlen von 0 bis 10⁹, die Version aus `[0-9A-Za-z.+-]`; jede Abweichung ergibt `None` für die ganze Datei, und der Abschnitt
  fehlt), zur Laufzeit wird nichts gezählt – im Docker-Image fehlt `tests/`. Gehört die Datei nicht zur Version des Programms, sagt die Seite es
  („Die Zahlen stammen aus Version …“)
- Migration auf 2.2.0 (`INVOICE_VERSIONS_SCHEMA`, Schema 2002): hängt `Invoices` fünf Spalten an (`InvoiceKind`,
  `CancelsInvoiceId`, `ReplacesInvoiceId`, `SentAt`, `DocumentVersion`; die ersten drei sind für den Stornobeleg
  angelegt – `InvoiceKind` und `CancelsInvoiceId` nutzt er seit 2.2.1, `ReplacesInvoiceId` die Ersatzrechnung seit 2.2.2,
  beide ohne Schemaänderung und `ReplacesInvoiceId` ohne Index) und legt `InvoiceCorrections`, `InvoiceDocumentVersions` und den eindeutigen Index
  auf `CancelsInvoiceId` an – ebenfalls nach der Sicherung und in einer Transaktion. Der Bestand behält überall
  NULL (`DocumentVersion` NULL = Fassung 1), nichts wird umgedeutet. Frische und migrierte Datenbank haben dieselbe
  Spaltenreihenfolge (`InvoiceCol`, Index 50–54). Die beiden neuen Tabellen haben bewusst keinen Fremdschlüssel:
  Protokoll und Fassungen überleben das Löschen eines Entwurfs
- Migration auf 2.3.0 (`DATEV_KEY_SCHEMA`, Schema 2003): hängt `Bookings.DatevKey` (Index 23, NULL = keiner mitgebracht) und
  `CompanyDetails.DatevConsultantNo`/`DatevClientNo` an – nach der Sicherung und in EINER Transaktion – und berichtigt `TaxKeys`
  (`TAX_KEY_CORRECTIONS`: 100/120/121/122/490 – eine abweichende Zeile wird an ihrer Stelle überschrieben, eine fehlende angelegt, eine richtige
  bleibt unberührt; nur wenn die Tabelle Zeilen hat, die leere legt `_seed_tax_keys` vollständig an). Der Bestand behält überall NULL, nichts
  wird umgedeutet; frische und migrierte Datenbank haben dieselbe Spaltenreihenfolge
- Migration auf 2.4.0 (`VAT_METHOD_SCHEMA`, Schema 2004): hängt `Contacts.VatMethod` (Text, NULL = nicht festgelegt) als letzte Spalte
  an – nach der Sicherung und in EINER Transaktion. Der Bestand behält überall NULL, nichts wird umgedeutet; frische und migrierte
  Datenbank haben dieselbe Spaltenreihenfolge. Die Kontakt-Abfrage (`_CONTACTS_QUERY`) führt sie als Index 34
- Migration auf 2.5.0 (`BUSINESS_ONLY_SCHEMA`, Schema 2005): hängt `Accounts.BusinessOnly` (`INTEGER NOT NULL DEFAULT 1`: 1 = reines
  Geschäftskonto, 0 = gemischt genutzt) als letzte Spalte an – nach der Sicherung und in EINER Transaktion. **Der Bestand bekommt überall 1**
  (alle Konten gelten als reine Geschäftskonten, die Importe laufen wie bisher), nichts wird umgedeutet; frische und migrierte Datenbank
  haben dieselbe Spaltenreihenfolge. `Accounts`-Zeilen werden überall positionsbasiert gelesen (`fetch_accounts`/`get_account_by_id`:
  `IsCash` Index 6, `SKRAccount` Index 7) – die neue Spalte hängt hinten an (Index 8), nichts verschiebt sich. Geschrieben wird nur 0 oder
  1 (`insert_account(business_only=True)`, `update_account(business_only=None)`: `None` = unverändert; sonst `ValueError`), gelesen
  streng (`is_business_only`, `account_allows_selection`). Gepflegt im Formular Stammdaten → Bankkonten (Kästchen „Reines Geschäftskonto“,
  Standard angehakt; bei der Kasse nicht): Ein nicht angehaktes Kästchen schickt nichts, deshalb sagt das versteckte Feld
  `business_only_field=1`, dass das Formular das Kennzeichen führt – nur dann liest der Handler es (genau `1` = angehakt), sonst
  bleibt der Wert. Es ändert keine Buchung, keine Auswertung und keinen Export, nur die Oberfläche des Kontoauszug-Imports
- Migration auf 2.6.0 (`DOCUMENT_READINGS_SCHEMA`, Schema 2006): legt nur die Tabelle `DocumentReadings` (siehe *Belegerkennung*) und den Index
  `idx_documentreadings_gross` auf `GrossMinor` an – nach der Sicherung und in der Transaktion der Umstellung (`_ensure_document_readings_schema`, ohne
  eigenen Schritt in `_run_migrations`; eine frische Datenbank legt sie beim Anlegen an). Keine vorhandene Tabelle ändert sich, der Bestand bekommt keine
  Lesungen und nichts wird umgedeutet: Ein Beleg mit Datei gilt als ungelesen, bis er gelesen wird
- Anzeige auf der About-Seite

---

## Governance

- **Owner:** NobseVomBerg
- **Letzte Aktualisierung:** 2026-11-21
- **Modell-Versionen:** Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5 (Stand November 2026);
  `settings.json` und Agents nutzen Aliase (`fable`, `opus` …), die der neuesten Version folgen
- **Bei Modell-Updates:** Modell-Routing oben, `.claude/settings.json` (`modelSettings`
  nennt die volle Opus-ID) und `.claude/agents/` prüfen

Fragen? → Memory updaten oder neuen Punkt hier dokumentieren.
