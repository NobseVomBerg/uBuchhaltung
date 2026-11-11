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

- Commits nur im Hauptthread, nicht in den Agents. Format
  `typ(bereich): deutsche Kurzbeschreibung`, z. B. `fix(einvoice): …`,
  `chore(version): 1.6.19 – …`
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
- Jeder Push (alle Branches) führt `.github/workflows/tests.yml` aus:
  Anonymisierungs-Guard + pytest

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
  `fetch(url, {method: 'POST'})`, das Footer-JS hängt das Token an) und Abmelden
  (`POST /logout` neben `/login` in `_auth_routes_*`); GET → jeweils 405
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
  Ausgabe (200000 Zeilen bzw. 20 MB, danach gekürzt). Diese Sperren nicht aufweichen
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
- **Wiederherstellen** (`server/backup.py`): Ein 7-Zip-Archiv wird vor dem Entpacken
  aufgelistet (`-t7z`, keine Links, keine absoluten oder `..`-Pfade), dann nach
  `backup/restore_*` entpackt, geprüft (keine Symlinks, Datenbank lesbar und nicht neuer
  als das Programm) und erst danach ersetzt. Der vorherige Stand wird nach
  `backup/replaced_*` verschoben, nicht vorab gelöscht, und erst nach Erfolg entfernt
  (`replaced_*` nie automatisch aufräumen). Über Dateisystemgrenzen wird kopiert statt
  umbenannt; beim Überschreiben kommt `buch.db` zuletzt; „vorher löschen“ mit einer
  Nur-Datenbank-Sicherung wird abgelehnt. Sicherung und Wiederherstellung sperren sich
  je Datenverzeichnis (`_exclusive`). Diese Reihenfolgen nie umdrehen

### Buchungssätze
- Bankbewegung = Zeile `BookingType='bank'`; ihre Buchungssätze sind Kinder
  (`entry`, `ParentBooking_ID`) und tragen selbst kein `Account_ID`
- Drei Formen, eingeordnet an EINER Stelle (`db/matching.classify_entry`), die EÜR
  (`get_euer_data`) und DATEV-Export gemeinsam nutzen: Zweckkonto vorne
  (`COA` = Erlös/Aufwand, Gegenkonto = Geldkonto oder leer), Geldkonto vorne
  (v. a. WISO-Import) und Umbuchung (beide Seiten kontiert, keine liquide). Die
  Masken prüfen die Richtung noch selbst (`_plan_split_rows`, `is_bank_effective`)
- Vorzeichen: Bei Zahlungen trägt der Betrag die Richtung der Geldbewegung
  (Einnahme +), egal auf welcher Seite das Zweckkonto steht. Bei Umbuchungen heißt
  positiv: `COA` im Soll, Gegenkonto im Haben. Fehlt die liquide Seite, bleibt es
  eine Zahlung (wie `is_bank_effective`)
- DATEV: Das Soll/Haben-Kennzeichen bezieht sich auf das Feld „Konto“ und folgt
  dieser Einordnung; der BU-Schlüssel folgt der Richtung der Geldbewegung. Die
  BU-Schlüssel sind fachlich noch nicht gegen einen echten Stapel geprüft
  (`docs/aufgaben.md`) – nicht als richtig voraussetzen
- Zahlungskonten: EÜR, DATEV-Export und Buchungsmaske (beim Bearbeiten) nehmen dieselbe
  Menge (`get_payment_coa_ids`), an der auch die Importe das Vorzeichen ausrichten: die
  SKR-Konten der eigenen Bank- und Kassenkonten – die Kasse der App (SKR04 1600) zählt wie
  ein Bankkonto über ihr eigenes Konto, nicht über einen Bereich – plus der feste Bereich
  der Importe (`DEFAULT_LIQUID_ACCOUNTS`): 1000–1099 und das Verrechnungskonto 1460
  (Geldtransit: WISO bucht darüber aufgeteilte Zahlungen und Anlagen-Umbuchungen).
  1000–1099 ist historisch (SKR03-Kassenbereich) und bleibt, weil vorhandene Bestände ihr
  Vorzeichen daran ausgerichtet haben – nicht ändern, bevor die Richtung beim Import
  festgeschrieben wird (`docs/aufgaben.md`). Ob ein Satz in die EÜR kommt, entscheidet die
  Sektion in `get_euer_data` – Kind einer Bank- oder Kassenbewegung; eigenständig auf einer
  Kasse oder (ohne Kontofilter) auf 1460; Umbuchung zwischen Anlage- und Erfolgskonto –,
  nicht die Kontenmenge; jede Buchung zählt dabei höchstens einmal (Schlüssel: ihre ID).
  `is_bank_effective` und die Split-Summe rechnen weiter nur mit den eigenen Bankkonten
  (`get_bank_coa_ids`)
- Kasse = SKR04-Konto 1600 als eigenes Konto: Eine neu angelegte Kasse
  (`ensure_kasse_exists`, `insert_account`) bekommt 1600, das Konto wird bei Bedarf
  nachgelegt (`ensure_cash_coa`). Vorhandene Kassen behalten ihre Nummer (leer oder 1460);
  1460 ist das Verrechnungskonto, keine Kasse. Hat die Kasse keine SKR-Nummer, ist 1600
  kein Zahlungskonto
- AfA (`book_depreciation`): Umbuchung „Aufwand an Anlagekonto“ (direkte Abschreibung),
  Betrag positiv, ohne `Account_ID`, je Anlage und Jahr nur einmal (Sperre in einer
  Transaktion). Aufwandskonto = Erfolgskonto (4000–7999), Anlagekonto = Konto 1–999, nach
  dem Abgang der Anlage keine AfA mehr. „Gebucht“ heißt: die Buchung gibt es (oder
  WISO-`booked`). Löschen der Buchung setzt das Jahr auf `planned` zurück, `delete_asset`
  lehnt bei gebuchter AfA ab. In der EÜR (Sektion 3, ohne Steuer), nicht im Dashboard – die
  AfA ist kein Geldfluss
- Masken schreiben nur, was sie führen (`update_booking_fields`) – Gegenkonto,
  Kategorie und fremde Kontakte dürfen beim Speichern nicht verloren gehen

### Demo-Modus & Screenshots
- `UBUCHHALTUNG_DEMO=1` (`demo.py`): Demodaten liegen **ausschließlich unter
  `data/users/demo/`** und werden nur geladen, wenn dieses Verzeichnis fehlt oder leer
  ist und es keinen Benutzer `demo` gibt – diese Garantie nie aufweichen. Solange die
  Variable gesetzt ist, arbeitet jeder Besucher als `demo` (kein Administrator); `demo`
  ist wie `tests` ein reservierter Benutzername. Demodaten (`demo_seed.py`,
  `seed_data/test/demo_scenario.json`) entstehen über die App-eigenen Code-Pfade,
  relativ zum Startdatum; pytest-Testdaten bleiben davon getrennt
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

### Kontoauszug-Import (CAMT.053 + PDF)
- Ein Weg für alle Formate: Parser in `importers/` liefern `BankStatement` (`base.py`) →
  `server/upload_handler.py` → Vorschau `server/import_preview.py` (Duplikate, Warnungen) → bestätigen
- CAMT (`importers/camt.py`): Storno-Richtung = `CdtDbtInd` (ISO 20022); Saldoprüfung exakt in
  Decimal/Minor Units, nie als float-Summe; DTD/Entities werden abgelehnt, ZIP nur im Speicher mit
  Grenzen, Beträge ≥ 10¹² sind Fehler – diese Schutzmaßnahmen nicht aufweichen
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
- Der Datenbankweg ist genauer: `MOV_FINACC_ACCRECORDS.ACCOUNTINGID` nennt die
  Zusammengehörigkeit von Teilbuchungen ausdrücklich (→ `Bookings.SourceGroup`),
  liefert die Anlagenverwaltung mit und umfasst alle Jahre
- WISO rechnet intern in **SKR03**; `BAS_FINACC_PLAN.SKR04` liefert die
  Umschlüsselung, selbst angelegte Unterkonten erben sie über
  `FINT_ACCOUNTS.BASEACCOUNTNO`. Konten ohne Zuordnung werden **gemeldet, nicht
  geraten**; fehlende SKR04-Konten legt der Import mit WISOs Bezeichnung an
- **Klammer eines Belegs ist die Beleg-Nr. + Datum**, nicht die `SourceGroup`:
  WISO vergibt je Verrechnungskonto-Buchung eine eigene `ACCOUNTINGID`. Die
  Quellgruppe ist damit feiner und greift nur, wo eine Beleg-Nr. fehlt
- **CSV und Datenbank nacheinander:** Der Datenbank-Import (`_insert_wiso_bookings`) übernimmt eine CSV-Buchung
  (`entry` ohne `SourceGroup`, `AutoMirror`, `Account_ID`) nur, wenn Datum, Betrag, Konto, Beleg-Nr. **und ein
  nicht leerer Text** gleich sind (Leerraum/`casefold`; der latin-1 gelesene WISO-Text wird dafür nach cp1252 umgesetzt) und das
  Gegenkonto nicht widerspricht; gleiches Gegenkonto geht vor, dann die kleinste ID, jede Zeile höchstens einmal.
  Gleicher Schlüssel + Text bei verschiedenen Gegenkonten ist mehrdeutig → eine Zeile ohne Gegenkonto bleibt liegen.
  Grundsatz: **im Zweifel neu anlegen und melden – nie aufweichen**; ähnliche, nicht übernommene Buchungen stehen
  als `possible_duplicates` auf der Seite. Übernommen wird per UPDATE (im Replay-Log; `adopted_rows`); die
  Ersatzsuche ohne Konto nur bei gleichem Gegenkonto – und nicht, wenn sich zwei Sätze des Imports nur im Konto
  unterscheiden
- Bedienung über *Verschiedenes* → „Import aus der Datenbank"; die Prüfung
  (`WisoDatabase.check`) bricht vor dem Schreiben ab, wenn die WISO-Version
  andere Tabellen/Spalten hat
- **Anlagen:** Anschaffungswert und Verkaufserlös stehen beide **netto** in den
  Büchern. Der Erlös trägt in WISO keine `INVENTORYID` – gemeinsam ist ihm mit
  dem Anlagegut nur der Abgangstext; ist er nicht eindeutig, bleibt das Feld
  leer statt falsch
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
- Versendete Rechnungen (Status sent/partial_payment/overdue/paid) werden von
  „PDF + E-Rechnung“ nicht neu erzeugt (`overwrite_sent` nur beim Eintritt in „versendet“)
- **Erzeugt** wird nur an zwei Stellen: über „PDF + E-Rechnung“ im Formular und
  automatisch beim Wechsel auf Status **versendet** (ab da ist die Rechnung
  eingefroren). Eine bereits versendete Rechnung wird nicht überschrieben
- **Heruntergeladen** wird immer die abgelegte Datei (`/invoice/pdf_download`,
  `/invoice/xml_download`) – nie eine frisch erzeugte. Fehlt sie, kommt 404 mit
  kurzem Klartext, den das Frontend direkt anzeigt
- **Angebote** gelten genauso: `/quote/pdf_download` liefert nur die abgelegte PDF
  (`quotes/<Jahr der Erzeugung>/<Angebots-Nr.> <Kunde>.pdf`), sonst 404 „Kein PDF
  vorhanden – bitte zuerst erzeugen.“. **Erzeugt** wird nur per POST
  (`/invoice/pdf_generate`, `/quote/pdf_generate`; GET → 405) – die Oberfläche
  erzeugt zuerst und lädt dann. Der Stundenzettel (`/worktime/pdf`, GET) schreibt
  nichts auf die Platte (`generate_worktime_pdf(store=False)`)
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
  im Header `X-Filename` (Umlaute!)

### Testing
- Vor PR: lokal starten (`python main.py`), Feature testen
- Wenn Bug reproduzierbar, direkt über DB debuggen
- SQL-Queries gezielt checken (z.B. `SELECT COUNT(*) ... GROUP BY ...`)
- **Tests schreiben nie ins echte `data/`:** `tests/conftest.py` gibt jedem Test eine
  eigene Datenwurzel (`userctx.DATA_ROOT` unter `tmp_path`); ein Session-Wächter
  (`tests/data_guard.py`) schlägt an, wenn doch etwas dort landet
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
  2.1.x → 2001); Datenbanken aus 1.x tragen 0 bis 6. Eine Datenbank mit höherer Zahl
  als das Programm wird nicht geöffnet (`db.SchemaTooNewError`) – nie zurückstempeln
- Sonstige Änderungen ohne DB-Bezug: nur `PATCH` +1; große Umbrüche: `MAJOR` +1,
  `SCHEMA_STEP` und `PATCH` auf 0
- Anzeige auf der About-Seite

---

## Governance

- **Owner:** NobseVomBerg
- **Letzte Aktualisierung:** 2026-11-10
- **Modell-Versionen:** Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5 (Stand November 2026);
  `settings.json` und Agents nutzen Aliase (`fable`, `opus` …), die der neuesten Version folgen
- **Bei Modell-Updates:** Modell-Routing oben, `.claude/settings.json` (`modelSettings`
  nennt die volle Opus-ID) und `.claude/agents/` prüfen

Fragen? → Memory updaten oder neuen Punkt hier dokumentieren.
