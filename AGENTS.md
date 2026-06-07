# AGENTS.md

Pragmatische Hinweise für KI-/Coding-Agenten in diesem Repository.

## Ziel

- Änderungen möglichst klein, präzise und nachvollziehbar halten
- Business-Logik im `db/`-Package zentral pflegen (Domänen-Mixins)
- UI-Rendering in `server/pages_*.py` lassen

## Code-Schwerpunkte

- `db/`: Datenmodell, Import, Matching, EÜR-Ableitung (Domänen-Mixins; Einstieg via db-navigate-Skill, Schema in `db/schema.py`)
- `server/pages_dashboard.py`: Dashboard inkl. EÜR-Tabellen
- `server/pages_transactions.py`: Transaktionssicht (bank + entry)
- `server/pages_miscellaneous.py`: WISO-Import, SQL-Konsole
- `document_parser.py`: Bank-PDF-Parser (u.a. VBR, DKB)

## Wichtige Domänenregeln

- Bookings unterscheiden `BookingType='bank'` und `BookingType='entry'`
- Verknüpfung Bank↔Entry über `ParentBooking_ID`
- Bei WISO-Original-Import gilt:
  - `TaxRate` aus `TaxKeys` (BU-Schlüssel)
  - `TaxAmount` aus Brutto abgeleitet
  - Spezialfall `4405 -> 4400`: 19% setzen, auch ohne BU-Schlüssel
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

## Nicht-Ziele

- Keine großflächigen Refactorings ohne konkreten Anlass
- Keine Umbenennungen nur aus Stilgründen
- Keine Änderungen an Seed-Daten ohne fachliche Begründung
