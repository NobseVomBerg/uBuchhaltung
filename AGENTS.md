# AGENTS.md

Pragmatische Hinweise für KI-/Coding-Agenten in diesem Repository.

## Ziel

- Änderungen möglichst klein, präzise und nachvollziehbar halten
- Business-Logik im `db/`-Package zentral pflegen (Domänen-Mixins)
- UI-Rendering in `server/pages_*.py` lassen

## Code-Schwerpunkte

- `db/`: Datenmodell, Import, Matching, EÜR-Ableitung (Domänen-Mixins; Modulüberblick in `.claude/skills/db-navigate/SKILL.md`, Schema in `db/schema.py`)
- `server/pages_dashboard.py`: Dashboard inkl. EÜR-Tabellen
- `server/pages_transactions.py`: Transaktionssicht (bank + entry)
- `server/pages_miscellaneous.py`: Seite „Daten“ (WISO-Import, DATEV-Export, Sicherung); SQL-Konsole und DB-Export: `server/pages.py` (About)
- `importers/`: Kontoauszug-Import – CAMT.053 (`camt.py`) und Bank-PDFs (`vbr.py`, `dkb.py`), gemeinsames Modell in `base.py`; `safe_xml.py` liest XML aus Uploads sicher (CAMT und Belegerkennung)
- `receipt_reading/`: Belegerkennung – E-Rechnung (XML) und PDF-Text lesen; reines Paket (kein `db/`, kein `server/`), gelesen wird je Beleg im Kindprozess (`worker.py`)
- `server/receipt_reader.py`, `server/pages_receipts.py`: Lesen auslösen, Lesung anzeigen, Vorschläge am Beleg; `db/document_readings.py`, `db/receipt_matching.py`: Lesung (`DocumentReadings`) und Vorschläge Beleg → Buchung
- `document_parser.py`: Belegablage, SQL-Audit-Log

## Wichtige Domänenregeln

- Bookings unterscheiden `BookingType='bank'` und `BookingType='entry'`
- Verknüpfung Bank↔Entry über `ParentBooking_ID`
- Belegerkennung: Eine Lesung (`DocumentReadings`) ist eine Lesung der Datei, kein Inhalt des Belegs – nichts wird ohne Klick verknüpft, gebucht oder am Beleg geändert
- Bei WISO-Original-Import gilt:
  - `TaxRate` aus dem BU-Schlüssel: zuerst `datev_keys.key_rate` (datumsabhängig), sonst `TaxKeys`; der Schlüssel steht als `Bookings.DatevKey` am Satz
  - `TaxAmount` aus Brutto abgeleitet
  - Spezialfall `4405 -> 4400`: 19% setzen, aber nur ohne BU-Schlüssel (leer oder 0)
- EÜR im Dashboard basiert auf Bookings:
  - Netto-Aggregation (`Amount - TaxAmount`)
  - Virtuelles 3806 nur aus USt-Anteilen der Einnahmen
  - Virtuelle 1401/1406 aus Vorsteuer-Anteilen der Ausgaben
  - 3160/3720/3740 separat als "Sonstige Ausgaben"

## Doku-Pflege

Bei Änderungen an Import, Matching oder Dashboard-EÜR immer mitpflegen:

1. `README.md` (Feature-Überblick)
2. `PARSER_README.md` (Import-/Parser-Details)
3. `DB_MODEL.md` (Datenfluss und fachliche Logik)

Offene Aufgaben stehen in `docs/aufgaben.md` – Erledigtes dort löschen.

Verbindliche Projektregeln stehen in `CLAUDE.md` (Kern) und `docs/regeln/` (je Bereich);
Betriebs- und Konfigurationsänderungen in `DEPLOYMENT.md` mitpflegen.

## Nicht-Ziele

- Keine großflächigen Refactorings ohne konkreten Anlass
- Keine Umbenennungen nur aus Stilgründen
- Keine Änderungen an Seed-Daten ohne fachliche Begründung
