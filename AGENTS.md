# AGENTS.md

Hinweise für KI-/Coding-Agenten in diesem Repository.

- **Verbindliche Regeln:** `CLAUDE.md` (gilt für jede Änderung, auch wenn Du kein Claude bist) und je Bereich
  `docs/regeln/*.md` – vor einer Änderung die passende Datei lesen (Tabelle in `CLAUDE.md`).
- **Nachschlagen:** Datenmodell und Datenfluss in `DB_MODEL.md`, Leser für Kontoauszüge, WISO-Textexport und Belege in
  `PARSER_README.md`, Betrieb in `DEPLOYMENT.md`, Modul-Landkarte von `db/` in `.claude/skills/db-navigate/SKILL.md`.
- **Offene Aufgaben:** `docs/aufgaben.md` (Erledigtes löschen).

## Doku-Pflege

Ändert sich Verhalten, ziehen mit: die Regeldatei des Bereichs in `docs/regeln/`, bei Tabellen und Spalten `DB_MODEL.md`,
bei Importformaten `PARSER_README.md`, bei Betrieb und Konfiguration `DEPLOYMENT.md`, bei sichtbaren Funktionen `README.md`.

## Nicht-Ziele

- Keine großflächigen Refactorings ohne konkreten Anlass
- Keine Umbenennungen nur aus Stilgründen
- Keine Änderungen an Seed-Daten ohne fachliche Begründung
