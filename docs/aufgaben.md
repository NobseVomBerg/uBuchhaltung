# Offene Aufgaben

Kurzliste, öffentlich. Erledigtes wird gelöscht, nicht abgehakt – die Historie
steht in `git log`. Eine Zeile je Punkt; Details gehören in Commit oder Issue.

## Repo & Veröffentlichung
- Ersten GitHub-Release `v1.6.16` veröffentlichen (Releases → Draft a new release → Generate release notes)
- `main` per Ruleset schützen: Löschen/Force-Push sperren, PR + Status-Check `pytest` verlangen
- GitHub-Einstellungen: Beschreibung, Topics, Social-Preview-Bild, Discussions
- Unraid Community Applications: `ca_profile.xml` ergänzen, Template auf ca.unraid.net/submit validieren
- awesome-selfhosted: frühestens 4 Monate nach dem ersten Release; Eintrag schreibt der Owner selbst (Listenregel: keine KI-generierten Beiträge)
- Docker-Demo (`-e UBUCHHALTUNG_DEMO=1`) nach dem ersten Release einmal im echten Container prüfen

## Funktionen
- Kontoauszüge im CAMT.053-/MT940-Format (bisher nur PDF: DKB, Volksbank Rottweil)
- ZUGFeRD zusätzlich zur XRechnung
- Rechnungen/Angebote mit gemischten Steuersätzen (7 % und 19 % je Position) – bisher ein Satz je Beleg
- Umsatzsteuer-Voranmeldung (ELSTER)
- GoBD-Modus, je Datenverzeichnis einstellbar (Ersteinrichtung fragt: Steuerbüro oder selbst?); vor Umsetzung Design (architect):
  - Festschreibung abgeschlossener Zeiträume, Korrektur nur per Storno/Gegenbuchung, kein Löschen
  - Änderungsprotokoll (wer/wann/alt/neu), manipulationssicher; SQL-Konsole nur lesend oder aus
  - Gilt auch für Kasse und Fahrtenbuch; Abschalten nur protokolliert

## Fehler & Technik
- Rechnungsformular (`/invoice?id=…`) läuft bei 1440 px Breite rechts aus dem Viewport
- Invoices-Zeilen werden per Spaltenindex gelesen (u. a. `server/pages_invoice.py`) – bricht bei Schemaänderungen, besser Spaltennamen
- CI testet nur Python 3.13, README nennt 3.10+ → Matrix ergänzen oder Angabe anpassen
- KI-Doku entschlacken: `CLAUDE.md`, `AGENTS.md`, `.claude/agents.md` auf Doppelungen prüfen
