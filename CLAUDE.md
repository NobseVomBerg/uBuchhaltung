# uBuchhaltung – Hinweise für Claude Code

Buchhaltung und Rechnungen mit Web-Oberfläche: Python-Standardbibliothek (`http.server`, kein Framework), SQLite,
serverseitig erzeugtes HTML + JS, `buch.css`. `server/` (Routen in `app.py`, Handler in `handlers.py`, Seiten als
`pages_*.py`), `db/` (Datenbank-Package aus Domänen-Mixins, Schema in `db/schema.py`; Landkarte: db-navigate-Skill),
`export/` (PDF, E-Rechnung, DATEV), `importers/`, `receipt_reading/`. Datenmodell und Datenfluss: `DB_MODEL.md`.
Offene Aufgaben: `docs/aufgaben.md` (Erledigtes löschen, Historie = `git log`). Entwicklungsserver: `python main.py`
(Port 8080, darf jederzeit beendet und neu gestartet werden).

Diese Datei lädt jede Sitzung und jeder Agent. Sie hält nur, was für **jede** Änderung gilt. Die Regeln eines Bereichs
stehen in `docs/regeln/` – vor einer Änderung dort die passende Datei lesen und sie einem Agenten im Auftrag nennen.

## Regeln nach Bereich (`docs/regeln/`)

| Datei | Wann lesen |
|---|---|
| `sicherheit.md` | Routen, Türsteher, Anfragegrößen, Formular- und Adresswerte, Ausgabe/Maskierung, SQL-Konsole, Anmeldung, Logos, E-Mail |
| `sicherung.md` | Sicherung, Wiederherstellung, Download/Löschen von Sicherungen, Seite *Daten*, About |
| `buchungen.md` | Form eines Satzes (`EntryForm`), Vorzeichen, Zahlungskonten, Masken, Bank-Editor, Abgleich, Kasse, AfA, Stammdaten löschen, Standardkonten |
| `zahlungen.md` | Rechnungszahlung, Forderung, Erstattung, Skonto, Kleinunternehmer-Erlöse, Zahlungsvorschläge, „Zuordnung neu aufbauen“ |
| `rechnungen.md` | Rechnungen und Angebote: Dokumente, E-Rechnung, Steuersätze je Position und aus den Stammdaten, Sperre, Berichtigen, Status, Storno, Nummern |
| `auswertungen.md` | DATEV-Export, Dashboard, BWA, EÜR, Summen- und Saldenliste, Liquidität |
| `import.md` | Kontoauszug-Import (CAMT, PDF, CSV), WISO-Textexport und WISO-Datenbank |
| `belege.md` | Belegdateien, Belege verknüpfen, Belegerkennung und Vorschläge |
| `zeiten-fahrten.md` | Arbeitszeiten, Fahrtenbuch, Stundenzettel |
| `oberflaeche.md` | Layout der Beleg-Seiten, Zeitraum-Menüzeile |
| `werkzeuge.md` | Diagnose-MCP-Server, Demo-Modus und Demodaten |
| `ci.md` | Workflows, Docker-Image, Release, CLA-Prüfung |
| `versionierung.md` | Schema-Migrationen, `project_stats.json` |

Neue Entscheidungen gehören in die Datei ihres Bereichs, nicht hierher. Regeln beschreiben den Stand, keine Geschichte:
kein „seit Version …“, kein „bis x.y war …“ – das steht im `git log`. Was ein Test festhält, reicht als Verweis auf den Test.

## Modell-Routing

- **Fable** ist der Hauptthread (Effort `high`, `xhigh` für schwieriges Coding oder Architektur, `max` nur ausnahmsweise):
  Anfrage verstehen, Architektur, unklare Entscheidungen, Planung, Review.
- **Opus**: Plan-Agent, urteilslastige general-purpose-Aufgaben, `financial-security-reviewer`.
- **Sonnet**: die Coding-Agents in `.claude/agents/` – `test-agent` (pytest schreiben, starten, reparieren) und
  `implementer-agent` (einen fertig entschiedenen Plan umsetzen).
- **Haiku**: nur der `scout-agent` – Nachschlagen (wo definiert/benutzt, Doku, Log, Git-Historie) und reine Prüfläufe
  (Anonymisierungs-Guard, pytest, CI-Status), die berichten statt reparieren. Statt des eingebauten Explore.
- Nach Änderungen an Geldrechnung, SQL-Abfragen oder Exporten (DATEV, XRechnung/ZUGFeRD, PDF) prüft der
  `financial-security-reviewer`.
- Kein Subagent auf Fable: eingebaute Agents nie ohne Modell aufrufen (Plan → `opus`, general-purpose → `sonnet`, bei
  urteilslastiger Arbeit `opus`), Workflow-`agent()` ebenso mit `opts.model`; nie `model: "fable"`; Haiku nur über den
  scout-agent und nie für Code.
- Advisor (Fable): Coding-Agents fragen nur, wenn derselbe Fehler zweimal wiederkommt oder vor einem schwer umkehrbaren
  Schritt; scout-agent und Reviewer nie; der Hauptthread nur auf Wunsch des Nutzers oder nach zwei gescheiterten Ansätzen.
- Nach einem Lauf mit mehreren Agents ihren Token-Verbrauch je Modell berichten.

## Arbeitsregeln

- Anrede: Der Owner wird geduzt („Du“), nie gesiezt – in jeder Antwort, auch in Zwischenständen und Rückfragen.
- Commits nur im Hauptthread. Format `typ(bereich): deutsche Kurzbeschreibung`, z. B. `fix(einvoice): …`,
  `chore(version): 2.9.2 – …`. Autor ist der Owner (`git commit --author=…` mit der Identität aus
  `git log --author=NobseVomBerg -1 --format='%an <%ae>'`), Claude steht als `Co-Authored-By` darunter; keine
  `Claude-Session`-Zeile.
- Gezielt lesen: erst Grep/Glob, keine Reads über 2000 Zeilen.
- Skills: `code-review` für Diffs, `security-review` für Sicherheits-Patches, `run` zum Prüfen in der laufenden App.

## Grundregeln für jede Änderung

**Geld und Daten**
- Beträge in Minor Units (`money.SCALE` = 4) bzw. `Decimal`, nie als Summe von `float`. Beträge aus Dateien nur über
  `money.parse_amount` (DKB-CSV: `parse_grouped_amount`), aus Formularen über `_finite_number` – nie `float()`.
- Nichts still falsch: Was sich nicht sicher lesen oder zuordnen lässt, bleibt leer und wird gemeldet – lieber ein
  Hinweis zu viel als eine falsche Buchung. Kein Import und keine Maske deutet Bestand still um.
- Schreiben in EINER Transaktion (`BEGIN IMMEDIATE`), Prüfung im selben Stand; Konten, die angelegt werden könnten,
  vorher in eigenen Verbindungen auflösen; innerhalb einer Transaktion keine öffentliche Methode, die selbst schreibt
  (dafür die Bausteine mit `tx`/`conn`). Replay-Log erst nach dem Commit.
- Absätze mit „nicht aufweichen“, „nicht lockern“ oder „nie“ in `docs/regeln/` sind Schutzregeln. Ändern nur mit
  ausdrücklicher Entscheidung des Owners.

**HTTP und Ausgabe** (Einzelheiten: `docs/regeln/sicherheit.md`)
- Türsteher `_gate()`; Aktionen mit Seiteneffekt nie per GET (`_ACTION_PATHS`, `_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`,
  `_UPLOAD_PATHS`; im Frontend `appPost`/`appConfirmPost`/`fetch(…, {method: 'POST'})`).
- Bodys nur über `_read_body(limit)`/`_check_body_size`; IDs aus der Adresse über `_int_param`/`_id_param`, aus
  Formularen über `_form_id`/`_form_int`, aus JSON über `_json_positive_id`.
- Jeder Wert wird beim Einsetzen maskiert: HTML mit `html.escape(…, quote=True)`, im `<script>` nur `pages.script_json`,
  in Adressen im Inline-JS `quote(…, safe='')`; im Browser `textContent`/`createElement`, nie `innerHTML` mit Daten.
- Nie Ausnahmetext, Serverpfad oder Dateiinhalt in einer Antwort: feste Sätze, der volle Fehler ins Server-Log.
- Dateipfade bestimmt der Server, nie der Client; gespeicherte Pfade nur über die Prüfstellen (`stored_receipt_path`,
  `stored_invoice_path`, `realpath` + `commonpath`).
- Im Demo-Modus nimmt die App keine Datei eines Besuchers an (jeder neue Upload-Weg braucht die Sperre).

**Oberfläche**
- Styles nur in `buch.css`, vorhandene Klassen wiederverwenden. `buch.css` wird lange gecacht: **CSS-Änderung = Versionssprung.**
- Texte kurz, keine Steuerberatung, keine GoBD-Zusicherung, keine Marken- oder Produktnamen in Seiten-, PDF- und Testtexten.

**Öffentliches Repo**
- README oben: Zweck, Schnellstart, Überblick; Grenzen ehrlich benennen, nichts versprechen.
- Markennamen Dritter (z. B. WISO) nur zur Bezeichnung von Importformaten, mit Markenhinweis; keine vergleichenden Aussagen.
- Strategisches, Rechtliches, private Aufgaben und Beschreibungen noch offener Schwachstellen gehören ins private Repo
  `NobseVomBerg/uBuchhaltung-Private` (lokal `../uBuchhaltung-Private`, in Cloud-Sessions per add_repo; Übergaben in
  `uebergaben/`). Fehlt es, nicht danach fragen. Testdaten nur erfunden.

## Tests

- `python -m pytest` (ein Prozess); die CI läuft mit `-n 4`. Vor dem Commit dazu `python tests/check_anonymized.py`.
- Tests schreiben nie ins echte `data/` (`tests/conftest.py` gibt jedem Test eine eigene Datenwurzel, `tests/data_guard.py`
  wacht). Skripte außerhalb von pytest lenken `userctx.DATA_ROOT` selbst um.
- Kein „heute“ beim Import: nie in Modulkonstanten, `parametrize`, Standardwerten oder Fixtures über `function`-Scope;
  wo die App einen Haken hat (`today=`, `invoice_storno._today`), ein festes Datum (`tests/test_clock_at_import.py`).
- Tests laufen auf mehreren Prozessen: eigene Datenwurzel, Server nur auf Port 0, keine festen Dateinamen außerhalb von
  `tmp_path`, kein Zustand für den nächsten Test, Test-IDs ohne Uhr oder Zufall.
- Testläufe liegen unter `data/tests/` (`tmp/`, `cache/`). Keine Punkt-Verzeichnisse im Projektordner.
- Schnappschuss-Tests (Golden-Dateien): Ändert sich ein Schnappschuss, ist der Code zu ändern, nie die Datei.

## Versionierung und Release

- Eine Quelle: `version.py` (`MAJOR.MINOR.PATCH`). **Ein Versionssprung auf `main` ist der Release** (Tag, Image,
  GitHub-Release); ohne Sprung entsteht nur `:edge`. Jede App-Änderung bekommt ihren PATCH-Sprung, reine Doku-/CI-Änderungen
  keinen.
- Schemaänderung (`db/schema.py`): `SCHEMA_STEP` +1, PATCH auf 0; die Datenbank trägt `MAJOR × 1000 + SCHEMA_STEP`. Eine
  neuere Datenbank wird nie geöffnet oder zurückgestempelt. Migration nach der Sicherung, in EINER Transaktion, der Bestand
  behält NULL, frische und migrierte Datenbank sind gleich (`docs/regeln/versionierung.md`).
- Bei jedem Versionssprung `python tools/project_stats.py` ausführen und `project_stats.json` mit committen.
- Nach einem Merge mit Versionssprung den Release-Lauf nachsehen – scheitern dort die Tests, gibt es weder Image noch Release.
- Actions sind auf einen Commit festgelegt (`@<40 Hex-Zeichen>  # vX.Y.Z`, `tests/test_workflow_pins.py`).

## Governance

Owner: NobseVomBerg. Bei Modell-Updates Modell-Routing, `.claude/settings.json` und `.claude/agents/` prüfen.
