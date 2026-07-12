# Mitwirken

Danke für dein Interesse, zu diesem Projekt beizutragen!

## Lizenz deiner Beiträge

Das Projekt steht unter der **AGPL-3.0-or-later** und wird zusätzlich unter
einer kommerziellen Lizenz angeboten (Dual Licensing). Damit das möglich
bleibt, müssen alle Beiträge durch das **Contributor License Agreement
([`CLA.md`](CLA.md))** abgedeckt sein.

Mit dem Absenden eines Pull Requests stimmst du den Bedingungen des CLA zu.

## Workflow

1. Issue anlegen oder ein bestehendes kommentieren, bevor du größere
   Änderungen beginnst.
2. Fork erstellen und einen Feature-Branch anlegen.
3. Änderungen umsetzen. **Jede neue Quelldatei bekommt die SPDX-Kopfzeilen**
   (siehe bestehende `.py`-Dateien als Vorlage).
4. Pull Request gegen den `main`-Branch öffnen und kurz beschreiben, was und
   warum geändert wurde.

## Code-Stil

- Halte dich an den bestehenden Stil des Projekts.
- Kleine, fokussierte Commits sind leichter zu reviewen als ein großer.
- Tests nur mit anonymisierten Daten (`seed_data/test/`); ein automatischer
  Check (`tests/check_anonymized.py`) erzwingt das.
