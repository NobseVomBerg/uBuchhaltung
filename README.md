# PyBuch

Eine webbasierte Buchhaltungsanwendung für kleine Unternehmen und Selbstständige.

## Überblick

PyBuch ist eine schlanke, webbasierte Buchhaltungssoftware in Python. Sie verwaltet Belege, Konten, Buchungen und nutzt den deutschen Standardkontenrahmen (SKR). Bankbewegungen (`BookingType='bank'`) und Buchungssätze (`BookingType='entry'`) werden in einer gemeinsamen Tabelle geführt und automatisch verknüpft.

## Features

### 1. Belege-Verwaltung (`/belege`)
- Übersicht aller Belege mit Nummer, Datum, Dateiname, Pfad und Zusatzinfos
- Hinzufügen, Bearbeiten, Löschen
- Many-to-Many-Verknüpfung mit Buchungen (BookingDocuments)
- UNIQUE-Constraint in `BookingDocuments` auf `(Booking_ID, Document_ID)` verhindert doppelte Verknüpfungen

### 2. Buchungen (`/transactions`)
Zentrale Verwaltung aller Buchungstransaktionen:
- **Zwei Buchungstypen**:
  - `bank` – echte Bankbewegung (Import aus Kontoauszug oder WISO-Tabellen-Export)
  - `entry` – Buchungssatz (aus WISO-Original-Export oder manuell)
- **Automatische Verknüpfung**: Bank↔Entry über `ParentBooking_ID` (via `link_bank_to_entries()`)
- **Merged Display**: Bankbuchungen zeigen verknüpfte Entry-Daten (SKR-Konto, Steuersatz, Belegnr.)
- **Status-Badges**: ✓ verknüpft / offen für Bankbuchungen; ✓ für Einzelbuchungen und Split-Gruppenköpfe mit vollständig gebuchten Kinder-Einträgen
- **Doppik-Filter**: Reine Gegenbuchungen auf Bankkonten (z.B. SKR 1810) werden automatisch ausgeblendet
- **Erweiterte Buchungsfelder**:
  - Buchungsdatum und Steuerdatum (getrennt)
  - SKR-Sollkonto (`COA_ID`) und Gegenkonto (`CounterCOA_ID`)
  - Steuersatz und berechneter Steuerbetrag
  - Multi-Währung (EUR, USD, GBP, CHF)
  - Kontakt-, Kategorie- und Dokumentnummer-Zuordnung
  - Fremde IBAN (`ForeignBankAccount`)
- **Split-Buchungen**: Gruppierung via `BookingGroup_ID`
- **Erweiterte Filter**: Datum, Konto, Kunde, Währung, Betragsbereich – alle kombinierbar

### 3. Konten-Verwaltung (`/konten`)
- Übersicht aller Bank-/Kassenkonten (Name, Inhaber, IBAN, BIC, BankName)
- SKR-Kontozuordnung (`SKRAccount`) für automatische Doppik-Erkennung
- Kasse als Sonderform (automatisch beim Start angelegt, nicht löschbar)
- CRUD-Funktionalität

### 4. Standardkontenrahmen (SKR) (`/skr`)
- SKR 03/04 (Deutschland), SKR 07 (Österreich)
- **Automatisches Seeding**: Standard-SKR04-Konten beim DB-Erstellen aus `seed_data/`
- CRUD-Funktionalität, UNIQUE(Framework, AccountNumber)
- CSV-Upload für Massenimport

### 5. Kontakte (`/masterdata/contacts`)
Normalisiertes 3NF-Schema (4 Tabellen):
- **Entitätstypen**: Unternehmen und Personen mit separaten Formularen
- **Kontakttypen**: Kunde, Lieferant, Eigene Daten, Versicherung, Sonstiges
- **Logo-Verwaltung**: File-Picker mit Live-Vorschau
- **Tabellen**: Contacts (Basis) + ContactAddresses (1:n) + CompanyDetails (1:1) + PersonDetails (1:1)

### 6. Rechnungserstellung (`/invoice`)
Professionelle Rechnungserstellung mit PDF, E-Mail-Versand und XRechnung:
- **Multi-Company-Support**: Eigene Firma wählbar, dynamisches Logo
- **Snapshot-Prinzip**: Verkäufer- und Käuferdaten werden bei Erstellung als Kopie gespeichert
- **Positionstabelle**: Beliebig viele Positionen, Artikelverzeichnis-Integration
- **PDF-Generierung** (`export/pdf_invoice.py` auf Basis von `export/pdf_core.py`): A4-Layout, Logo, Umlaute, dreispaltiger Footer
- **E-Mail-Versand**: SMTP, PDF-Anhang, Empfänger aus Kontakten
- **XRechnung XML-Export** (`export/xrechnung_invoice.py`): EN 16931 konform
- **Rechnungsstatus**: Entwurf → Finalisiert → Versendet → Bezahlt (+ Teilzahlung, Überfällig, Storniert)
- **Nummernkreise**: Format YY[Buchstabe][Präfix]### mit Auto-Inkrementierung
- **Zahlungsverknüpfung**: Rechnungen mit Bankbuchungen verbinden, Teilzahlungen

### 7. Dashboard (`/`)
- Finanz-Statistiken auf Basis von Bank- und Entry-Buchungen (inkl. Kasse)
- Monatlicher Verlauf für Einnahmen, Betriebsausgaben und Privatentnahmen
- Jahresübersicht mit Saldo, kontobasiert filterbar
- EÜR-Ansicht mit drei Blöcken:
  - Betriebseinnahmen
  - Betriebsausgaben (offizielle EÜR)
  - Sonstige Ausgaben (separat ausgewiesen)
- Steueranteile in der EÜR:
  - Virtuelles 3806 nur aus USt-Anteilen der Einnahmen
  - Virtuelle Vorsteuerkonten 1401/1406 für Ausgaben
- **Kasse-Buchungen** (Section 2b): Direkte Entry-Buchungen auf Kassenkonto ohne COA-Spiegel (`CounterCOA=NULL`) werden separat erfasst und fließen korrekt in EÜR und Dashboard ein

### 8. Mahnwesen (`/invoice/reminders`)
- 3-Stufen-Mahnsystem (Erinnerung → 1. Mahnung → Inkasso)
- Fälligkeitsvorschau (7 Tage)
- Farbcodierung nach Dringlichkeit

### 9. Kategorien (`/categories`)
- Hierarchische Kategorien (Parent-Child)
- Für private Belege (Immobilien, Versicherungen, ...)

### 10. Artikelverzeichnis (`/articles`)
- Artikelstamm: Bezeichnung, Einheit, Nettopreis, Steuersatz
- Active-Flag, direkte Integration in Rechnungen

### 11. Nummernkreise (`/masterdata/numberranges`)
- Format: YY[Buchstabe][Präfix]### (z.B. 26R001)
- Typen: Ausgangsrechnungen, Belegnummern
- Jahreswechsel-Support

### 12. Split-Buchungen (`/bookinggroups`)
- Gruppierung mit Soll/Haben-Validierung
- Beschreibung und erwarteter Gesamtbetrag

### 13. Anlagenverzeichnis (`/assets`)
Vollständiges Anlagenmanagement mit gesetzeskonformer AfA:
- **BMF-Kategorien**: 30 vordefinierte Kategorien aus `seed_data/`
- **AfA-Methoden**: Linear (anteilig), Degressiv (25%), GWG (≤ 800 €)
- **Live-Vorschau**: JavaScript-basierte Echtzeit-AfA-Berechnung
- **Inventarnummern**: Auto-generiert `INV-YY-###`
- **Buchungsintegration**: AfA-Buchung als Bookings-Eintrag
- **Erweiterungen**: Sub-Anlagen über Parent_ID

### 14. Sonstiges (`/miscellaneous`)
- **Datenbank-Übersicht**: Tabellenstatistiken
- **DB-Export**: INSERT-Statements nach `data/db-export.sql`
- **WISO Import**: Automatische Format-Erkennung (Original + Tabellen-Export)
  - BU-Schlüssel → Steuersatz-Lookup aus TaxKeys-Tabelle
  - Automatische TaxAmount-Berechnung (Brutto → MwSt-Anteil)
  - 4405→4400 wird auch ohne BU-Schlüssel mit 19% USt angereichert
  - Tabellen-Export: optionale `Konto-Nr. / IBAN`-Spalte (6- und 7-spaltig)
  - Tabellen-Export Matching: bank+entry-Paare ohne Belegnummer (Privatentnahmen, Gebühren) werden als Einheit erkannt und aktualisiert
  - Nach Import: automatische Bank↔Entry-Verknüpfung (`link_bank_to_entries()`)
- **DATEV-Export** (`export/datev.py`): Buchungsstapel als DATEV-CSV (nur Entry-Buchungen)
- **SQL-Konsole**: Direkte SQL-Ausführung (Entwickler-Tool)

### 15. About (`/about`)
Informationen über die Anwendung.

---

## Projektstruktur

```
PyBuch/
├── main.py                    # Entry Point – Webserver starten
├── db/                        # Datenbank-Package (CRUD, Import, Linking, Seeding)
│   ├── __init__.py            #   Database-Klasse (Komposition aller Mixins)
│   ├── core.py                #   Verbindung, Init-Guard, Geld-Grenze, coa_id
│   ├── schema.py              #   alle CREATE TABLE / Schema-Setup
│   ├── bookings.py · matching.py · invoices.py · contacts.py · …
│   └── (weitere Domänen-Mixins: assets, accounts, wiso_import, reporting …)
├── document_parser.py         # PDF-Parser für Kontoauszüge (u.a. VBR, DKB)
├── export/                    # Ausgabe-/Export-Generatoren (Package)
│   ├── pdf_core.py            #   Gemeinsame PDF-Primitive (Builder, Logo, Escaping)
│   ├── pdf_invoice.py         #   PDF-Rechnungsgenerierung
│   ├── pdf_worktime.py        #   PDF-Stundenzettel (Arbeitszeiten)
│   ├── xrechnung_invoice.py   #   XRechnung XML (EN 16931)
│   └── datev.py               #   DATEV-Buchungsstapel-Export (CSV)
├── email_sender.py            # E-Mail-Versand (SMTP)
├── buch.css                   # Stylesheet (inkl. Dark Mode)
├── README.md                  # Diese Datei
├── DB_MODEL.md                # Detailliertes Datenbankmodell
├── PARSER_README.md           # Dokumentation Parser & CSV-Import
├── AGENTS.md                  # Leitfaden für KI-/Coding-Agenten im Repo
├── requirements_parser.txt    # Python-Dependencies (Parser)
├── seed_data/                 # Initialisierungsdaten (JSON)
│   ├── tax_keys.json          # 50 DATEV-Steuerschlüssel (BU-Codes)
│   ├── asset_categories.json  # 30 BMF-AfA-Kategorien
│   ├── chart_of_accounts_skr04.json  # Standard-SKR04-Kontenrahmen
│   ├── private/               # Benutzerspezifisch (in .gitignore)
│   └── test/                  # Testdaten für Entwicklungs-DB
│       ├── test_accounts.json     # Bankkonten
│       ├── test_articles.json     # 50 Artikel (Tante-Emma-Laden)
│       ├── test_assets.json       # 22 Anlagegüter
│       ├── test_bookings.json     # Buchungen
│       ├── test_contacts.json     # 33 Kontakte (Kunden, Lieferant, Eigene)
│       ├── test_documents.json    # Belege
│       └── test_invoices.json     # 50 Rechnungen
├── server/                    # Modularer Webserver
│   ├── __init__.py            # Package init
│   ├── app.py                 # HTTP-Server mit Routing
│   ├── pages.py               # Gemeinsame HTML (Header, Footer, Nav)
│   ├── pages_dashboard.py     # Dashboard
│   ├── pages_invoice.py       # Rechnungen (Liste, Neu, Bearbeiten, Ansicht)
│   ├── pages_transactions.py  # Buchungen (Bank+Entry merged Display)
│   ├── pages_booking_groups.py # Split-Buchungen
│   ├── pages_contacts.py      # Kontaktverwaltung (4-Tabellen-Schema)
│   ├── pages_masterdata.py    # Stammdaten (Artikel, SKR, Bankkonten, Nummernkreise)
│   ├── pages_assets.py        # Anlagenverzeichnis
│   ├── pages_setup.py         # Ersteinrichtungs-Seite (First-Run)
│   ├── pages_receipts.py      # Belege (Upload, Bearbeiten)
│   ├── pages_miscellaneous.py # DB-Export, SQL-Konsole, WISO Import
│   ├── handlers.py            # POST-Handler (Formulare, PDF, Import)
│   └── upload_handler.py      # File-Upload mit PDF-Parsing
├── static/                    # Statische Dateien (Logos)
└── data/                      # Laufzeitdaten (in .gitignore)
    ├── buch.db                # SQLite-Datenbank
    ├── Belege/                # Hochgeladene Belege
    ├── Documents/             # Dokumente
    └── pending_imports/       # Temp. Import-Daten
```

> Datenbankschema: siehe [DB_MODEL.md](DB_MODEL.md)

---

## Installation und Start

### Voraussetzungen
- Python 3.x
- Pillow (für PDF-Logo-Einbettung): `pip install Pillow`
- `pdfplumber` (für PDF-Parsing im `document_parser.py`): `pip install pdfplumber` (siehe `requirements_parser.txt`)

### Server starten

```bash
python main.py
```

Erreichbar unter `http://localhost:8080`. Die Datenbank wird automatisch erstellt inkl. aller Seed-Daten (SKR04-Konten, AfA-Kategorien, DATEV-Steuerschlüssel).

### E-Mail konfigurieren (optional)

```powershell
$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "ihre-email@gmail.com"
$env:SMTP_PASSWORD = "ihr-app-passwort"
```

### PDF-Parser aktivieren (optional)

```bash
pip install -r requirements_parser.txt
```

---

## Workflow

### Ersteinrichtung
1. `python main.py` starten → DB mit Seed-Daten wird erstellt
2. Bankkonten unter `/konten` anlegen (mit SKR-Zuordnung, z.B. SKR 1810)
3. Kontakte unter `/masterdata/contacts` erfassen
4. Ggf. eigene SKR-Konten unter `/skr` ergänzen

### Tägliche Nutzung
1. WISO-Export importieren unter `/miscellaneous` (Bank↔Entry automatisch verknüpft)
2. Kontoauszüge als PDF hochladen unter `/belege`
3. Buchungen prüfen und ergänzen unter `/transactions`
4. Rechnungen erstellen, PDF generieren, versenden

### Seed-Daten anpassen
- Standard-Daten in `seed_data/*.json` (wirken nur bei leerer Tabelle)
- Eigene SKR-Ergänzungen in `seed_data/private/` (in `.gitignore`, nicht im Repo)
- Testdaten in `seed_data/test/` (werden nur bei `load_test_seed_data()` geladen)

---

## Architektur

- **Webserver**: Python `http.server.BaseHTTPRequestHandler`
- **Datenbank**: SQLite mit `PRAGMA foreign_keys = ON`
- **Seeding**: JSON-Dateien aus `seed_data/` bei DB-Erstellung
- **PDF**: `export/pdf_core.py` (Builder) + `export/pdf_invoice.py` / `export/pdf_worktime.py` (A4, Logo)
- **Frontend**: Server-seitig generiertes HTML + JavaScript
- **Styling**: `buch.css` mit Dark Mode

### Server-Module

| Modul | Aufgabe |
|-------|---------|
| `server/app.py` | Routing, HTTP-Handler |
| `server/pages.py` | Gemeinsame HTML (Header, Nav, Footer, Konstanten) |
| `server/pages_dashboard.py` | Dashboard |
| `server/pages_invoice.py` | Rechnungen |
| `server/pages_transactions.py` | Buchungen (Bank+Entry merged) |
| `server/pages_booking_groups.py` | Split-Buchungen |
| `server/pages_contacts.py` | Kontakte |
| `server/pages_masterdata.py` | Stammdaten |
| `server/pages_assets.py` | Anlagen |
| `server/pages_setup.py` | Ersteinrichtungs-Seite (First-Run) |
| `server/pages_receipts.py` | Belege (Upload, Bearbeiten) |
| `server/pages_miscellaneous.py` | DB-Export, SQL-Konsole |
| `server/handlers.py` | POST-Handler |
| `server/upload_handler.py` | File-Upload |

---

## Sicherheitshinweise

- ⚠️ Keine Authentifizierung
- ⚠️ Keine Verschlüsselung
- ⚠️ Nur für lokale Verwendung – nicht für produktiven Internet-Einsatz

---

## Lizenz

Dieses Projekt ist für Lern- und Demonstrationszwecke erstellt.

## Autor

NobseVomBerg
