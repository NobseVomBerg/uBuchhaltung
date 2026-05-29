# PyBuch – Agent & Token-Optimierungs-Strategie

## Projekt-Überblick

**PyBuch** ist ein Python-basiertes Buchhaltungs-/Rechnungsverwaltungssystem mit Flask-Backend und Web-UI. Fokus: Finanz-Datenmanagement, SKR-Konten, Geschäftsvorfälle, PDF-Rechnungen.

- **Tech Stack:** Python (Flask), SQLite, Jinja2 Templates, CSS, HTML
- **Struktur:** `server/` (App-Logik), `db.py` (Datenbank), `buch.css` (Styling), Templates im `server/templates/`
- **Entwicklungs-Server:** `python main.py` (Port 8080)
- **Entscheidende Files:** `db.py` (Schema), `server/app.py` (Routes), `server/pages_*.py` (Features)

---

## Agent-Architektur für Token-Optimierung

Ziel: Hohe KI-Performance bei minimalem Token-Verbrauch – verhindert 5h-Limit-Breaks bei langen Sessions.

### Modell-Verteilung

| **Agent-Typ** | **Modell** | **Use-Case** | **Token-Budget/Session** |
|---|---|---|---|
| **Orchestrator/Planung** | Opus 4.8 | Architektur, Code-Design, komplexe Entscheidungen | ~20% (selektiv) |
| **Haupt-Development** | Sonnet 4.6 | Implementierung, Debugging, Test-Schreiben, Refactoring | ~60% |
| **Schnell-Analyse** | Haiku 4.5 | Code-Reviews, Fehleranalyse, Commits, Lint-Check | ~20% |

**Faustregel:** Erst Sonnet für die Arbeit einsetzen. Nur bei komplexen Architektur-Fragen oder verfahrenem Debugging zu Opus eskalieren.

---

## Task-Routing

### Wann nutze ich welchen Agent?

**Sonnet 4.6 (Standard-Entwickler)**
- Feature-Implementierung, Bugfixe, Refactoring
- Code lesen und ändern
- Tests schreiben & debuggen
- Meiste PyBuch-Arbeit landet hier
- **Start here**, es sei denn klar kein Sonnet-Fall

**Opus 4.8 (nur für komplexe Architektur)**
- Neu-Design von Modulen oder DB-Schema
- Trade-off-Analysen bei konfliktären Anforderungen
- Wenn Sonnet nach >30k Tokens "ich verstehe nicht" sagt
- Code-Review über größere Refactorings hinweg
- **Sparsam nutzen** – kostspieliger in Token/Zeit

**Haiku 4.5 (Sub-Tasks)**
- `code-review` Skill für kleine Diffs
- Commit-Message-Vorschläge
- Schnelle Code-Analyse (Fehler-Localisation)
- Linting & Style-Checks
- `security-review` auf Patches
- **Delegieren Sie diese Sub-Tasks** – sparen Kontext im Haupt-Agent

---

## Token-Optimierungs-Strategien

### 1. **SubAgent-Delegation maximieren**
```
Problem: Du lieferst einem 70k-Token-Sonnet mit 100k Kontext-Fenster = 30k für neue Arbeit
Lösung: Splits Analyse/Review zu Haiku, Planung zu Opus
Ersparnis: ~15-20k Tokens pro Session
```

**Konkret:**
- Groß-Features: `Agent subagent_type=Explore` für Init-Analyse
- Code-Review: `code-review` Skill (nutzt Haiku intern)
- Commit-Erstellung: Haiku via Sub-Agent
- Fehler-Debugging: Haiku analysiert first, Sonnet fixiert nur nachdem lokalisiert

### 2. **Kontext-Priorisierung**
Jeder Agent kriegt nur **was er braucht**:
- Sonnet bei Feature X: nicht die ganze DB-Struktur, nur relevante Tables/Routes
- Opus bei Arch-Frage: Summary statt komplette Files
- Haiku bei Review: nur der Diff, nicht die ganzen Dateien

**Settings:** Read-only Tools (Grep, Glob, file Read) sind erlaubt und kaum teuer → nutzen für gezielt Kontext suchen statt blindlings alles laden.

### 3. **Streaming & Caching nutzen**
- Long-Context (z.B. komplette Datei mit >500 Zeilen) → als Zusammenfassung referenzieren
- Bei sich wiederholenden Pattern (z.B. "SKR-Konten-Migration"): Opus schreibt 1x Pattern auf, Sonnet nutzt es 3x
- Prompt Caching in API-Calls (falls später REST-Integration) automatisch nutzen

### 4. **Self-Limiting Rules**
- **Kein Sonnet >40k Token** in einer Turn ohne Pausen
- **Kein Opus ohne Frage**, die >10k Token sparen würde
- **Keine File-Reads >2000 Zeilen** (kürzen oder summarize)
- **Commits nur vom initialen Agent** – nicht hin-und-her zwischen Agenten

---

## Workflow-Beispiele

### Szenario 1: Kleine Bugfix (Standard)
```
Nutzer: "Auf Seite X zeigt sich Y falsch"
→ Sonnet 4.6 mit Kontext auf X & Related Routes
→ Sonnet liest Bug, schreibt Fix, testet lokal
→ Bei Verunsicherung: Sonnet delegiert `code-review` zu Haiku
→ Sonnet schreibt Commit
✓ ~8-12k Tokens total, 5-10 Min
```

### Szenario 2: Neue Feature mit unklarem Design
```
Nutzer: "Ich will eine Funktion für X, aber wie das am besten passt, weiß ich nicht"
→ Opus 4.8: 10 Min Design-Review (5k Token)
  - Schaut aktuellen Code, schlägt Architektur vor
→ Sonnet 4.6: Implementierung nach Opusplan (20-25k Token)
  - Focused, kein Rumraten mehr
→ Opus Review des Resultat (optional, 2-3k Token)
✓ ~27-33k Tokens, 30 Min — statt 50k+ wenn Sonnet rumprobiert hätte
```

### Szenario 3: Komplexer Refactor (Multi-Agent)
```
Nutzer: "Räume Pages-Modul auf, splitte in Router + Handler"
→ Opus 4.8: Architektur-Plan (10k Token)
  - definiert neue Struktur, Modul-Grenzen
→ Agent Haiku: `Explore` = Analyse bestehender Pages (5k Token, parallel)
  - findet alle pages_*.py Files, ihre Imports, ihre Route-Definitionen
→ Sonnet 4.6: Implementierung nach Plan (25-30k Token)
  - Haiku-Analyse als Input, implementiert strukturiert
→ Sonnet/Haiku: Code-Review des neuen Moduls (5-8k Token)
✓ ~45-53k Tokens (vs. ~70-80k wenn alles Sonnet)
✓ Opus-Plan verhindert Irrwege; Haiku-Analyse spart Sonnet-Kontext
```

---

## Best Practices für diese Session

1. **"Sonnet-first" Mentalität**
   - Start immer mit Sonnet, es sei denn Frage ist offensichtlich Arch/Design

2. **Nutze SubAgents für Paralleles**
   - Große Codebasis-Analyse? → `Agent Explore`
   - Mehrere unabhängige Reviews? → Parallele Haiku-Agenten
   - Nicht sequenziell alles auf einen Agenten ballern

3. **Explizite Token-Grenzen**
   - Wenn eine Turn >25k wird: Punkt machen, neue Session
   - Pro Feature: 1 Session, max 50k, sonst Split
   - Monats-Limit des Pro-Plans nicht übersehen (check regelmäßig)

4. **Dokumentation statt Kontext**
   - Knifflige Anforderung? → in Memory speichern, nicht jedes Mal erklären
   - Architektur-Entscheidungen → CLAUDE.md updaten, nicht re-erläutern

5. **Skill-Nutzung**
   - `code-review` für Diffs (Haiku)
   - `run` zum Verifizieren (lokal)
   - `security-review` für Sicherheits-Patches
   - `consolidate-memory` wenn Memory >10 Dateien

---

## Spezifika für PyBuch

### Styling
- Alle Styles gehören in `buch.css` (nicht inline in HTML)
- Wiederverwenden statt neue Klassen-Inflationen
- (Siehe Memory: `styling-css-guidelines.md`)

### Development-Server
- Port 8080 kann jederzeit gekillt & neu gestartet werden
- (Siehe Memory: `server-may-be-killed.md`)

### Testing
- Vor PR: lokal starten (`python main.py`), Feature testen
- Wenn Bug reproduzierbar, direkt über DB debuggen
- SQL-Queries gezielt checken (z.B. `SELECT COUNT(*) ... GROUP BY ...`)

---

## Governance

- **Owner:** NobseVomBerg
- **Letzte Aktualisierung:** 2026-05-29
- **Modell-Versionen:** Opus 4.8, Sonnet 4.6, Haiku 4.5 (Stand Mai 2026)
- **Bei Modell-Updates:** Diese Datei + Settings entsprechend updaten

Fragen? → Memory updaten oder neuen Punkt hier dokumentieren.
