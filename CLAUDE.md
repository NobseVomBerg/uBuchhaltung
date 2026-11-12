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
  rahmt sich nicht selbst ein (PDFs öffnen als eigene Seite in einem neuen Tab)
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
- **Die Form eines Satzes steht als Daten am Satz** (`Bookings.EntryForm`, seit 2.1.0;
  `db/matching.py`, Bankzeilen haben keine). Sechs Werte, je mit der Seite, auf die sich der
  Betrag bezieht: `purpose_first` (Zweckkonto vorne, Gegenkonto = Geldkonto oder leer; Betrag =
  Richtung auf dem Gegenkonto), `liquid_first` (Geldkonto vorne, v. a. WISO-Import; Richtung auf
  dem Konto), `transfer` (Umbuchung, beide Seiten kontiert, keine liquide; positiv = `COA` im
  Soll), `mirror_coa` / `mirror_counter` (Spiegel, beide Seiten Geldkonten; Betrag auf dem Konto
  bzw. dem Gegenkonto) und `mirror_legacy` (Spiegel des Bestands, Lesart unbekannt – gelesen wie
  vor 2.1.0: Gegenkonto-Seite, im Dashboard roh, in der Split-Summe über `is_bank_effective`).
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
- DATEV: Das Soll/Haben-Kennzeichen bezieht sich auf das Feld „Konto“ und folgt der Form
  (`soll_side`: steht bei positivem Betrag das Feld „Konto“ im Soll, ist es bei positivem Betrag S);
  der BU-Schlüssel folgt der Richtung der Geldbewegung und dem exakten
  Steuersatz (19, 7, 16, 5 %; jeder andere Satz '0'). Die BU-Schlüssel sind fachlich noch
  nicht gegen einen echten Stapel geprüft (`docs/aufgaben.md`) – nicht als richtig voraussetzen
- Zahlungskonten (`get_payment_coa_ids`): die SKR-Konten der eigenen Bank- und Kassenkonten –
  die Kasse der App (SKR04 1600) zählt wie ein Bankkonto über ihr eigenes Konto, nicht über
  einen Bereich – plus das Verrechnungskonto 1460 (`DEFAULT_LIQUID_ACCOUNTS`; Geldtransit: WISO
  bucht darüber aufgeteilte Zahlungen und Anlagen-Umbuchungen). Das ist die Menge, an der die
  Importe das Vorzeichen ausrichten und die Maske und `insert_booking` die Form **beim Schreiben**
  ableiten – und mit der eine Zeile ohne Form gelesen wird. **1000–1099 gehört nicht mehr dazu**
  (SKR04: Vorräte; früher der SKR03-Kassenbereich): Beide WISO-Importe melden Buchungen darauf
  (`stock_range_accounts`, Hinweis auf *Verschiedenes*: Buchungen gegen diese Konten werden als
  Umbuchung gelesen und erscheinen nicht in der EÜR), und `negative_gross` zählt jeden Satz mit negativem
  Brutto und genau einer liquiden Seite (dort bestimmt die Geldbewegung das Vorzeichen, ein Storno wird so
  wie eine Buchung gelesen – gleich, ob die Zahl ihr Vorzeichen dabei ändert; bis das festgeschrieben ist,
  `docs/aufgaben.md`).
  Ob ein Satz in die EÜR kommt, entscheidet die
  Sektion in `get_euer_data` – Kind einer Bank- oder Kassenbewegung; eigenständig auf einer
  Kasse oder (ohne Kontofilter) auf 1460; Umbuchung zwischen Anlage- und Erfolgskonto –,
  nicht die Form und nicht die Kontenmenge; jede Buchung zählt dabei höchstens einmal
  (Schlüssel: ihre ID). Verknüpfung und Split-Summe rechnen mit den eigenen Bankkonten
  (`get_bank_coa_ids`); ein Spiegel mit bekannter Lesart wirkt in der Split-Summe nach seiner
  Position auf dem Konto der Bankbewegung (`bank_effect`), im Dashboard aus Sicht der Kasse
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
  AfA ist kein Geldfluss
- Rechnungszahlung: `assign_invoice_payment` (`db/invoice_payments.py`) ist die einzige Stelle, die
  eine Zuordnung Rechnung ↔ Bankbewegung anlegt, `unassign_invoice_payment` die einzige, die sie
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
  könnte, werden vorher in eigenen Verbindungen aufgelöst – nie innerhalb. Nur Zahlungseingänge
  (Betrag > 0) auf versendete Rechnungen (`PAYABLE_STATUSES`), Bankkonto mit SKR-Nummer; frei ist
  `Betrag − MAX(Zuordnungen, bankwirksame Sätze)` (`_free_minor_sql`, überall dieselbe Formel – sie
  rechnet Spiegel roh, die Split-Summe dagegen über `bank_effect`, der einen Spiegel mit bekannter
  Lesart nach seiner Position zählt; beides ist bewusst nicht dasselbe). Der
  Zuordnungsbetrag wird auf Cent gerundet (halb auf), bevor er gekappt wird; die Steuer eines Teils ist
  anteilig (Cent), der schließende Teil bekommt den Rest der Rechnungssteuer abzüglich der Steuer der
  anderen Zuordnungen (markierte Sätze: die gebuchte, sonst – Altbestand, WISO – die anteilige).
  **Erlöskonto:** Es ergibt sich aus der Rechnung (`resolve_revenue_coa`) und wird NUR aus den markierten
  Sätzen gelernt, und nur in der Form, in der die App sie schreibt (Umbuchung „Wartekonto an Erlöskonto“:
  `COA` ist ein Wartekonto, gelernt wird das Gegenkonto; Einzelsatz „Erlöskonto an Bank“: das Gegenkonto
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
  `get_receivable_booking`). Stufe 7 des
  Auto-Abgleichs setzt sie erst auf `resolved`, wenn die Summe der Sätze „… an 10000“ mit gleicher
  Beleg-Nr. den Forderungsbetrag erreicht; `unassign` setzt das zurück. Die Forderung geht mit der
  letzten Zahlung, wenn der Status der Rechnung kein Forderungsstatus ist (`RECEIVABLE_STATUSES`, z. B.
  storniert) – gleicher Endzustand, egal ob erst storniert oder erst entfernt wird; **entfernt wird nur die
  Forderung, die die App selbst gebucht hat** (`_receivable_removal`: ohne `SourceGroup`, Wartekonto als
  Gegenkonto, keiner Bankbewegung zugeordnet), nie eine importierte oder von Hand gebuchte.
  Negative Zuordnungen kennt nur der WISO-Import (`link_invoice_to_transaction`, mit Vorzeichen, ohne
  Sätze). `find_payment_inconsistencies` prüft nur lesend und linear im Bestand (Seite *Verschiedenes*:
  nur auf Klick, `?check_payments=1`, nie beim Laden; Hinweis auf der Rechnungsseite); Zuordnungen auf
  Nicht-Bankbewegungen oder mit `SourceGroup`-Sätzen (WISO) löst `no_settlement`/`transfer_mismatch`
  nicht aus. Eine Reparatur gibt es nicht
- Masken schreiben nur, was sie führen (`update_booking_fields`) – Gegenkonto,
  Kategorie und fremde Kontakte dürfen beim Speichern nicht verloren gehen. **Öffnen und unverändert
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
  ganzen Zuordnung, und `delete_transaction` aus der Liste bleibt ein ausdrückliches Löschen
  (`tests/test_split_editor_removal.py`)

### Diagnose-MCP-Server
- `tools/mcp_ubuchhaltung.py`: nur Standardbibliothek, stdio; Claude Code startet ihn lokal,
  er fragt eine laufende Instanz (z. B. den Docker-Container) per HTTP ab
- Lesend per Konstruktion: genau ein SELECT ohne `;` über `/execute_sql`, nur lesende
  `pragma_`-Funktionen (`_READ_PRAGMAS`), kein WITH/RECURSIVE/zeroblob/randomblob; Seiten nur
  aus `_PAGE_ALLOWLIST` samt erlaubten Query-Schlüsseln – diesen Lesewächter (`check_select`,
  `check_page_path`) nicht aufweichen; neue Seiten und Schlüssel nur für GET-Handler, die
  nichts schreiben (Nachweis im Test)
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
  auf *Verschiedenes*). Weicht eine andere gespeicherte Form von der des frischen Imports ab, wird das nur
  gemeldet (`form_conflicts`). **Zeilen auf Konten 1000–1099 aus 2.0.x** (dort als Kasse gelesen, also mit
  einem anderen Betrag gespeichert: „6815 an 1050“ −10, „1050 an 1800“ +30) erkennen beide Wege am Betrag der
  alten Regel wieder (Zahlungskonten einschließlich 1000–1099): der Re-Import des Textexports mit einer
  zusätzlichen Duplikatsuche (gleiche Beleg-Nr., Datum, Konto, Gegenkonto, jede Zeile höchstens einmal, nicht
  die, die eine andere Zeile der Datei mit ihrem eigenen Betrag beansprucht), die Übernahme CSV → Datenbank
  über `WisoBooking.legacy_amount` unter denselben Schutzregeln wie sonst. Kein zweiter Satz, Form und Betrag
  der Altzeile bleiben, die Übernahme füllt nichts; der Zähler heißt `legacy_stock_rows` (Hinweis auf
  *Verschiedenes*)
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
- Versendete und stornierte Rechnungen (`db.invoice_lock.FROZEN_STATUSES`) werden von
  „PDF + E-Rechnung“ nicht neu erzeugt (`overwrite_sent` nur beim Eintritt aus Entwurf/abgeschlossen in
  „versendet“); liegt für sie noch keine Datei vor (Import, gescheiterte Erzeugung), darf sie erzeugt werden
- **Erzeugt** wird nur an zwei Stellen: über „PDF + E-Rechnung“ im Formular und
  automatisch beim Wechsel von Entwurf/abgeschlossen auf Status **versendet** (ab da ist die Rechnung
  eingefroren). Eine bereits versendete Rechnung wird nicht überschrieben; versendet ↔ überfällig erzeugt nichts
- **Sperre versendeter Rechnungen** (`db/invoice_lock.py`, seit 2.1.2) – **Zwischenstand** bis zum Stornobeleg
  (`docs/aufgaben.md`). Eine Stelle entscheidet
  (`check_changes`, Statusgruppen), und jeder Schreibweg auf eine Rechnung geht darüber: das Speichern
  (`Database.update_invoice_fields`, bei einer versendeten Rechnung nur aus `correct_invoice`) und der
  Statuswechsel (`Database.set_invoice_status`) – nie
  `update_invoice_status` (der ungeprüfte Baustein für Seed und Tests; Angebote: `set_quote_status`). Bearbeitbar ist alles bei
  `draft`/`finalized`. Bei `sent`/`overdue`/`partial_payment`/`partial`/`paid` sind Nummer, Rechnungsdatum,
  Kunde, eigene Firma, Positionen, Steuer, Summen, Fälligkeit, Skonto, Bank, Währung und Käufer-USt-IdNr.
  gesperrt: `/invoice/save` antwortet mit **409** und einem Klartext, nichts der Anfrage wird geschrieben.
  Änderbar bleiben der Zahlungshinweis (`PaymentTerms`, der Text unter den Positionen), die Anschrift,
  Bestellnummer, Leitweg-ID und Leistungsdatum – aber nur als **Berichtigung** (siehe dort). `cancelled`: gar
  nichts mehr. Geprüft wird gegen den gespeicherten Stand **in der Transaktion** (`BEGIN IMMEDIATE`, das
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
  vom Gespeicherten passiert nichts – auch ohne `documentVersion`.
  Sonst in EINER Transaktion (`BEGIN IMMEDIATE`, die Rechnung wird neu gelesen): Die Fassung, die die Maske
  geladen hat (`documentVersion`; `Invoices.DocumentVersion`, NULL = 1), muss die aktuelle sein, sonst 409 (fehlt
  sie oder ist sie veraltet, wird nichts geschrieben); PDF und XML wandern nach `Fassungen/<ID>/`
  (`archive_sent_documents`) und stehen mit Fassung, Pfad (relativ zu `invoices/`) und SHA-256 in
  `InvoiceDocumentVersions`, ihre Pfade in der Rechnung werden geleert; jedes geänderte Feld ist eine Zeile in
  `InvoiceCorrections` (`Action='correct'`: wer, wann, alt, neu, neue Fassung – bei `refreshAddress` je
  Anschriftsspalte eine); `DocumentVersion` zählt um eins hoch. Scheitert das Verschieben oder der Commit, liegen
  die Dateien wieder am alten Platz (409, nichts geändert). Danach erzeugt `handlers._correct_invoice` das Dokument
  der neuen Fassung – nur, wenn eines abgelegt war (ein Import bekommt Felder und Protokoll, kein Dokument).
  Scheitert das Erzeugen, bleibt die Berichtigung gespeichert, die Antwort trägt eine Warnung, und „PDF +
  E-Rechnung“ erzeugt die fehlende Datei (die Regel „eingefroren, aber keine Datei vorhanden“). **Die berichtigte
  Fassung trägt einen Hinweis** (PDF: Zeile vor der Positionstabelle und „Fassung: n“ im Kopf; CII BT-22; UBL
  `cbc:Note`; TypeCode 380 und Nummer bleiben): Er ergibt sich aus dem Stand der Datenbank
  (`correction_notice`: `DocumentVersion` > 1 und Berichtigungen zur aktuellen Fassung, keine Rückkehr auf
  Entwurf danach), nicht aus einem Parameter – auch ein später über „PDF + E-Rechnung“ erzeugtes Dokument trägt
  ihn, eine nach dem Zurückholen neu versendete Rechnung nicht. Zeitpunkte stehen in UTC, Dokumente und
  Oberfläche nennen die Ortszeit. Die stornierte Rechnung bleibt ganz gesperrt
- **Statusübergänge** (`db.invoice_lock.TRANSITIONS`; die Statusauswahl bietet nur die erlaubten Ziele,
  `set_invoice_status` lehnt den Rest mit Klartext ab – 409, „bezahlt“ ohne Zahlung 400): Entwurf ↔
  abgeschlossen frei. **Entwurf/abgeschlossen → versendet** („Versenden“) ist der einzige Übergang, der die
  Forderung bucht und das Dokument erzeugt. Versendet ↔ überfällig bucht und erzeugt nichts (auch bei einem
  Import ohne Forderung der App). Teilzahlung/bezahlt und der Rückweg setzt die Zahlungslogik
  (`next_payment_status`); „bezahlt“ von Hand nur mit mindestens einer Zahlung (Skonto), der Rückweg von
  bezahlt/Teilzahlung nach versendet/überfällig von Hand nur ohne Zahlung (verwaister Zustand).
  Versendet/überfällig → Entwurf/abgeschlossen („zurückholen“) nur ohne Zahlung; die Forderung wird entfernt
  und die versendeten Dokumente wandern nach „Fassungen“ (siehe dort).
  → storniert von Hand aus versendet/überfällig/Teilzahlung/bezahlt, mit dem bisherigen Buchungsverhalten
  (die Forderung bleibt, solange eine Zahlung daran hängt) – bis zum Stornobeleg. Storniert → versendet/
  überfällig bucht die Forderung neu und lässt ein vorhandenes Dokument stehen.
  **Status, Forderungsbuchung und (beim Zurückholen) das Ablegen der Dokumente laufen in EINER Transaktion**
  (`set_invoice_status`, `BEGIN IMMEDIATE` von der Prüfung bis zum Commit): Sie gelingen gemeinsam oder gar nicht;
  nur das Erzeugen des Dokuments beim Versenden folgt danach (`handle_update_invoice_status`). **Jeder gelungene
  Wechsel steht im Protokoll** (`InvoiceCorrections`, `Action='status'`, in derselben Transaktion; die
  Zahlungslogik `_recalc_invoice` protokolliert nicht), und `send` setzt `SentAt` beim ersten Versand (Importe
  lassen es NULL). Konten, die
  das Buchen anlegen könnte, werden vorher in eigenen Verbindungen aufgelöst (`_receivable_accounts`), nie
  innerhalb. **Die App entfernt nur die Forderung, die sie selbst gebucht hat** (`_receivable_removal`: ohne
  `SourceGroup`, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet) – beim Zurückholen, beim Storno von
  Hand und beim Entfernen der letzten Zahlung (`_drop_receivable`): nie ein Satz mit Quellklammer. Hängt an der
  Rechnung eine importierte, von Hand gebuchte oder an eine Bankbewegung gehängte Forderung oder ein Zahlungssatz,
  lehnt das Zurückholen mit Klartext ab (409, nichts geändert); Storno und Zahlungsentfernen lassen sie stehen
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
  das Feld `readonly` mit Hinweis). Eine Formatvorlage braucht **genau einen Zähler** (`{n}`, `{nn}` oder `{nnn}`,
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
  kurzem Klartext, den das Frontend direkt anzeigt. Die Fassungen liefert `GET /invoice/version_download?id=&file=`,
  rein lesend: `file` ist ein reiner Dateiname (kein Trenner, kein `..`), gefunden wird nur eine `.pdf`-/`.xml`-Datei
  in einem Ordner `Fassungen/<id>` genau dieser Rechnung (`path_in_documents`); alles andere – fremde ID, Angebot,
  Pfadanteil – ist 404
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
  2.1.x → 2001, 2.2.x → 2002); Datenbanken aus 1.x tragen 0 bis 6. Eine Datenbank mit höherer Zahl
  als das Programm wird nicht geöffnet (`db.SchemaTooNewError`) – nie zurückstempeln
- Sonstige Änderungen ohne DB-Bezug: nur `PATCH` +1; große Umbrüche: `MAJOR` +1,
  `SCHEMA_STEP` und `PATCH` auf 0
- Migration auf 2.2.0 (`INVOICE_VERSIONS_SCHEMA`, Schema 2002): hängt `Invoices` fünf Spalten an (`InvoiceKind`,
  `CancelsInvoiceId`, `ReplacesInvoiceId`, `SentAt`, `DocumentVersion`; die ersten drei sind für den Stornobeleg
  angelegt und noch ungenutzt) und legt `InvoiceCorrections`, `InvoiceDocumentVersions` und den eindeutigen Index
  auf `CancelsInvoiceId` an – ebenfalls nach der Sicherung und in einer Transaktion. Der Bestand behält überall
  NULL (`DocumentVersion` NULL = Fassung 1), nichts wird umgedeutet. Frische und migrierte Datenbank haben dieselbe
  Spaltenreihenfolge (`InvoiceCol`, Index 50–54). Die beiden neuen Tabellen haben bewusst keinen Fremdschlüssel:
  Protokoll und Fassungen überleben das Löschen eines Entwurfs
- Anzeige auf der About-Seite

---

## Governance

- **Owner:** NobseVomBerg
- **Letzte Aktualisierung:** 2026-11-12
- **Modell-Versionen:** Fable 5.2, Opus 5.5, Sonnet 5.5, Haiku 5.5 (Stand November 2026);
  `settings.json` und Agents nutzen Aliase (`fable`, `opus` …), die der neuesten Version folgen
- **Bei Modell-Updates:** Modell-Routing oben, `.claude/settings.json` (`modelSettings`
  nennt die volle Opus-ID) und `.claude/agents/` prüfen

Fragen? → Memory updaten oder neuen Punkt hier dokumentieren.
