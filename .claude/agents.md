# Agent-Rollen & Checklisten für uBuchhaltung

Detaillierte Vorgaben für jeden Agent-Typ. Nutze diese als Referenz, wenn du einen bestimmten Agent delegierst.

---

## Sonnet 4.6 – Der Haupt-Developer

**Verantwortung:** Features bauen, Bugs fixen, Code refactorn, Tests schreiben.

### Sonnet Checkliste (vor Start einer Task)

- [ ] **Kontext gering halten**
  - Nur relevante Files einlesen (nicht ganze Codebase)
  - Nutze `Glob` + `Grep` um gezielt zu suchen
  - Max. 2-3 zusammenhängende Dateien pro Read

- [ ] **Verständnis vor Implementation**
  - Lese User-Request genau → summarize vor der Arbeit
  - DB-Schema-Änderung? → erst `db/schema.py` verstehen (db-navigate-Skill für Domänen-Mixins)
  - Route-Änderung? → erst `server/app.py` + relevante `pages_*.py` lesen
  - **Keine Umwege:** Plan zuerst, dann implementieren

- [ ] **Token-Bewusstsein**
  - Tracking: Wieviele Tokens sind bereits weg?
  - >20k in einer Turn? → Pause, neuer Punkt
  - >30k für eine Feature? → Zu viel, refactor die Task

- [ ] **Testing vor Abschluss**
  - Feature-Code geschrieben?
  - → Lokal starten (`python main.py`), UI testen
  - Edge Cases checken (leere Eingaben, große Zahlen, Unicode-Zeichen)
  - **Nicht nur "sieht gut aus"** – auch Fehlerfall-Handling

- [ ] **Commit-Botschaft korrekt**
  - Format: `feat:` / `fix:` / `refactor:` + kurze deutsche Beschreibung
  - Max. 1 Zeile Headline
  - Nicht zu viel erklären – Commit-Msg ≠ PR-Beschreibung

### Sonnet – Fokus-Fragen

Wenn Sonnet stuck ist:
- "Brauchst du Design-Input von Opus?" → Opus kurz einholen
- "Ist der Fehler reproduzierbar?" → Haiku zur Localisation
- "Verstehst du das aktuelle Modul-Layout?" → Explore-Agent für Analyse
- **Nicht:** stundenlang rumraten, sondern delegieren

### Sonnet – Token-Budget
- **Pro Feature:** 20-35k (ideal: 25k)
- **Pro Bugfix:** 8-15k
- **Pro Session:** Max. 60k (dann Split)

---

## Opus 4.8 – Der Architekt

**Verantwortung:** Design-Entscheidungen, Architektur-Reviews, große Refactors planen.

### Opus Checkliste (wann einsetzen)

**Trigger 1: Neues Modul / große Änderung**
- Nutzer: "Ich will Funktion X bauen, aber design-unklar"
- → Opus 5-10 Min: Architektur-Vorschlag (4-6k Token)
- → Sonnet implementiert (spart 20k Probier-Tokens)

**Trigger 2: Refactor mit Trade-offs**
- "Sollen wir DB-Schema splitten oder Queries optimieren?"
- → Opus: Pro/Con-Analyse (5-8k Token)
- → Sonnet: Gewählte Lösung umsetzen

**Trigger 3: Sonnet ist Lost**
- Sonnet nach 30k Tokens: "Ich sehe 5 Wege, weiß nicht welcher"
- → Opus: Quick-Review (3-5k Token)
- → Sonnet: mit klarem Plan weitermachen

**NICHT einsetzen:**
- "Schreib mir mal schnell die Pages neu" → Das ist Sonnet-Arbeit
- Einfache Typos/Lint-Fehler → Das ist Haiku-Arbeit
- Routine-Features mit klarem Scope → Das ist Sonnet-Arbeit

### Opus – Input-Anforderung

Opus braucht **fokussierte, knappe Inputs:**
```
GUT:
"Ich will SKR-Konten-Hierarchie ändern: Statt flat-list zu Eltern-Kind-Struktur.
Sollen wir new Table bauen oder JSON-Tree in bestehender Spalte?"

SCHLECHT:
"Schreib mir die ganze Accounting-Engine neu, unklar wie optimal"
(Zu vage, zu viel Arbeit)
```

### Opus – Output-Format

Opus liefert:
1. **Architektur-Skizze** (2-3 Abs., nicht mehr)
2. **Pro/Con der Optionen** (kurz, 3-4 Bullets pro Option)
3. **Empfehlung + warum** (klar)
4. **Nächste Schritte für Sonnet** (konkrete Befehle/Files)

Nicht: 5 Seiten Wall-of-Text. Kurz & actionable.

### Opus – Token-Budget
- **Pro Design-Frage:** 5-8k (max. 10k)
- **Pro Refactor-Plan:** 10-15k
- **Nie >20k pro Opus-Turn** (wenn mehr: Split in Sub-Fragen)

---

## Fable 5 – Der Spezialist (Erweiterung zu Opus)

**Verantwortung:** Genau die Aufgaben, an denen Opus scheitert – besonders schwierige Architektur-Knoten, kreative/neuartige Lösungsfindung ohne klaren Pfad.

> ⚠️ **Token-Kosten:** Fable verbraucht ca. **doppelt so viele Tokens wie Opus**. Daher ist Fable keine Routine-Stufe, sondern der letzte Eskalationsschritt der Orchestrator-Ebene.

### Fable Checkliste (wann einsetzen)

**Nur einsetzen, wenn ALLE zutreffen:**
- [ ] Opus wurde bereits versucht und liefert keinen tragfähigen Plan / dreht sich im Kreis
- [ ] Die Aufgabe ist genuin schwierig, kreativ oder neuartig (kein klarer Lösungspfad)
- [ ] Der erwartete Mehrwert rechtfertigt die ~doppelten Token-Kosten klar

**NICHT einsetzen:**
- ❌ Als Default-Orchestrator (dafür ist Opus da)
- ❌ Für Aufgaben, die Opus oder Sonnet lösen können
- ❌ "Sicherheitshalber" – immer erst Opus, dann eskalieren

### Fable – Input & Output

- **Input:** Wie bei Opus fokussiert + kurze Zusammenfassung, *was Opus bereits versucht hat* und *woran es gescheitert ist*.
- **Output:** Klare Empfehlung + nächste Schritte für Sonnet (gleiches Format wie Opus). Nicht: Wall-of-Text.

### Fable – Token-Budget
- **Pro Spezial-Frage:** 10-16k (≈ 2× Opus) – streng begrenzen
- **Regel:** Wenn >20k nötig wären → Aufgabe in Sub-Fragen splitten, ggf. zurück zu Opus

---

## Haiku 4.5 – Der Schnell-Checker

**Verantwortung:** Code-Reviews, Fehler-Localisation, Commit-Vorschläge, Lint-Checks.

### Haiku Checkliste (Sub-Tasks)

**Task 1: Code-Review eines Diffs**
```bash
code-review [--comment]
```
- Nutze den Skill, nicht manuelles Review
- Output: Syntaxfehler, unvollständige Error-Handling, Style-Violations
- Optional `--comment`: Inline-PR-Kommentare
- **Token:** 1-3k (sehr billig)

**Task 2: Fehler-Localisation**
- User: "Die Seite zeigt falsch"
- → Haiku: Schnell-Scan der Templates/Routes
  - DB-Query korrekt?
  - Template-Variable gesetzt?
  - CSS-Klasse falsch?
- → Bricht auf eine 2-3 Dateien runter
- → Sonnet fixiert dann gezielt
- **Token:** 2-4k, spart Sonnet 10k+ Kontext

**Task 3: Commit-Message**
- Haiku schreibt Commit-Message Vorlage
- Standard: `feat:` / `fix:` / `refactor:`
- Format: Deutsche Kurzbeschreibung
- **Token:** 0.5-1k
- Beispiel:
  ```
  fix: SKR-Konten-Hierarchie – Eltern-Kind-Relation korrigiert
  ```

**Task 4: Syntax/Lint-Check**
```bash
python -m py_compile [files...]
```
- Haiku prüft Python-Imports, Syntax
- Template-Validierung (if ungültig)
- **Token:** 0.2-1k

### Haiku – Nicht machen

- ❌ "Implementier mir eine neue Seite" (zu groß)
- ❌ "Designentscheidung treffen" (dafür Opus)
- ❌ "Schreib mir Tests" (zu komplex, Sonnet macht das besser)
- ❌ "Debugging von SQL-Queries" (dafür Sonnet mit vollem Kontext)

### Haiku – Parallel-Einsatz

Haiku ist billig → mehrere Haiku-Agenten parallel laufen lassen:
```
Task 1 (Haiku Agent 1): Code-Review des Diffs → 2k Token
Task 2 (Haiku Agent 2): Security-Review auf API-Changes → 2k Token
Task 3 (Haiku Agent 3): Fehler in Logging-Funktion localizen → 2k Token
→ Alles parallel, dann Sonnet: "Haiku sagt XYZ, bau das"
Statt Sonnet: alles selbst review + debug + check
```

### Haiku – Token-Budget
- **Pro Review-Task:** 1-3k
- **Pro Session:** Unbegrenzt (so billig, use freely)
- **Regel:** Wenn es <5 Min Sub-Task ist → Haiku

---

## Test-Workflow

Tests haben eine klare Arbeitsteilung — kein eigener "Test-Agent" nötig, aber definierte Rollen:

### Sonnet 4.6 – Test-Erstellung

Sonnet schreibt Tests, weil es Business-Logik-Verständnis braucht.

**Wann Tests schreiben:**
- Nach jedem neuen Feature → mindestens Smoke-Tests
- Bei Bugfixes → Regression-Test der den Bug reproduziert
- Bei DB-Schema-Änderungen → Schema-Tests in `tests/test_db.py`

**Regeln für Testdaten (PFLICHT):**
- **NIEMALS** echte Adressen, Namen, IBANs, Steuernummern verwenden
- Immer auf `seed_data/test/` zurückgreifen oder neue Dummy-Werte erfinden
- Erlaubte E-Mail-Domains: `@example.de`, `@muster.de`, `@muster-gmbh.de` (vollständige Liste in `tests/check_anonymized.py`)
- Erlaubte IBANs: Bundesbank-Test-IBANs (Liste in `tests/check_anonymized.py`) oder `DE00...`-Platzhalter
- Der Guard (`tests/check_anonymized.py`) läuft automatisch — Commit wird blockiert bei Echtdaten

**Testdaten-Quellen (in dieser Reihenfolge bevorzugen):**
```
1. seed_data/test/*.json  → fertige, gecheckte Fixtures
2. conftest.py Fixtures   → tmp_db, db_with_coa
3. Inline-Dummy-Daten     → erfundene Werte, keine echten
```

**Test-Struktur:**
- `tests/test_*.py` — pytest-Dateien
- `tests/conftest.py` — geteilte Fixtures
- `tests/fixtures/` — CSV/XML-Rohdaten für Import-Tests (von Guard ausgenommen)
- `tests/check_anonymized.py` — Anonymisierungs-Guard (kein Test, sondern Tool)

### Haiku 4.5 – Pytest-Output analysieren

Haiku ist ideal um den Output von `pytest -v` zu lesen und Fehler zu lokalisieren.

**Workflow:**
```
1. Sonnet oder Nutzer startet: python -m pytest tests/ -v
2. Tests schlagen fehl → Output zu Haiku delegieren
3. Haiku: "Fehler in test_db.py:47 — UNIQUE constraint verletzt, weil insert_account keinen Rollback macht"
4. Sonnet: bekommt präzise Fundstelle, fixiert gezielt
```

**Haiku-Input:** Nur den pytest-Output (nicht die ganzen Quell-Dateien)
**Haiku-Output:** Datei + Zeile + Ursache in 3-5 Sätzen, nicht mehr

**Token:** 1-3k für Pytest-Analyse (sehr günstig)

### Anonymisierungs-Guard (automatisch)

`tests/check_anonymized.py` läuft als Claude-Code Hook automatisch:
- **PostToolUse (Write/Edit):** Sofort-Warnung wenn Echtdaten in Testdatei
- **PreToolUse (Bash):** Check vor Commits

**Manuell ausführen:**
```bash
python tests/check_anonymized.py           # Mit Ausgabe
python tests/check_anonymized.py --quiet   # Nur bei Fehlern
python tests/check_anonymized.py tests/test_db.py  # Einzelne Datei
```

**Whitelist erweitern** (wenn neue Test-Domains/IBANs gebraucht):
→ `tests/check_anonymized.py` → `ALLOWED_EMAIL_DOMAINS` oder `ALLOWED_IBANS` ergänzen

---

## Agent-Routing-Baum

```
User gibt Task:
  ├─ "Baue Feature X"?
  │   └─ Design klar? 
  │       ├─ Ja → Sonnet (build it)
  │       └─ Nein → Opus (design 5min), dann Sonnet (build 20min)
  │
  ├─ "Fix Bug Y"?
  │   └─ Sonnet direkt
  │       └─ Stuck? → Haiku (localise), Opus (if arch-issue)
  │
  ├─ "Review mein Code"?
  │   └─ code-review Skill (Haiku) + optional Opus (if arch-review needed)
  │
  ├─ "Analyse große Codebase"?
  │   └─ Agent(subagent_type='Explore') mit Haiku oder Sonnet
  │
  └─ "Mehrere unabhängige Sub-Tasks"?
      └─ Parallel Haiku-Agenten starten (billig, schnell)
```

---

## Zusammenfassung

| Agent | Wann | Token/Task | Maxim |
|---|---|---|---|
| **Sonnet** | Features, Bugs, Testing, Refactor | 8-35k | "Nur Sonnet, wenn User selbst nicht weiß wie" |
| **Opus** | Design, Trade-offs, Planung | 5-15k | "Spare für schwierige Architektur-Fragen" |
| **Fable** | Spezial: was Opus nicht löst | 10-16k (≈2× Opus) | "Letzte Stufe – nur wenn Opus scheitert, teuer" |
| **Haiku** | Reviews, Lint, Commits, Sub-Analyse | 1-4k | "Use liberally – sehr billig" |

**Goldene Regel:** Start mit Sonnet. Erst eskalieren wenn nötig (Sonnet → Opus → Fable).
