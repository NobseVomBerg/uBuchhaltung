# uBuchhaltung

Eine webbasierte Buchhaltungsanwendung für kleine Unternehmen und Selbstständige.

## Überblick

uBuchhaltung ist eine schlanke, webbasierte Buchhaltungssoftware in Python. Sie verwaltet Belege, Konten, Buchungen und nutzt den deutschen Standardkontenrahmen (SKR). Bankbewegungen (`BookingType='bank'`) und Buchungssätze (`BookingType='entry'`) werden in einer gemeinsamen Tabelle geführt und automatisch verknüpft.

## Features

### 1. Belege-Verwaltung (`/receipts`)
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

### 3. Konten-Verwaltung (`/masterdata/bankaccounts`)
- Übersicht aller Bank-/Kassenkonten (Name, Inhaber, IBAN, BIC, BankName)
- SKR-Kontozuordnung (`SKRAccount`) für automatische Doppik-Erkennung
- Kasse als Sonderform (automatisch beim Start angelegt, nicht löschbar)
- CRUD-Funktionalität

### 4. Standardkontenrahmen (SKR) (`/masterdata/skr`)
- Kontenrahmen-Auswahlliste beim Anlegen: SKR 03/04 (Deutschland), SKR 07 (Österreich),
  SKR 14/49/51/70 (Branchen) – Server validiert Rahmen und Kontonummer
- **Standard-Umschalter**: eigene Konten als Standard anlegbar; Standard-Konten werden
  nach Entfernen des Hakens editier- und löschbar (Referenz-Schutz bleibt)
- **Automatisches Seeding**: Standard-SKR04-Konten beim DB-Erstellen aus `seed_data/`
- CRUD-Funktionalität, UNIQUE(Framework, AccountNumber), Menü-Sichtbarkeit je Konto

### 5. Kontakte (`/masterdata/contacts`)
Normalisiertes 3NF-Schema (6 Tabellen):
- **Entitätstypen**: Unternehmen und Personen mit separaten Formularen
- **Kontakttypen**: Kunde, Lieferant, Eigene Daten, Versicherung, Sonstiges –
  Mehrfachzuordnung über ContactTypeLinks, Personen-Rollen über PersonRoles
- **Logo-Verwaltung**: File-Picker mit Live-Vorschau
- **Tabellen**: Contacts (Basis) + ContactAddresses (1:n) + CompanyDetails (1:1)
  + PersonDetails (1:1) + ContactTypeLinks (n:m) + PersonRoles (n:m)

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

### 9. Kategorien
- Hierarchische Kategorien (Parent-Child) für private Belege (Immobilien, Versicherungen, ...)
- Derzeit nur im Datenmodell (Tabelle `Categories`), noch ohne eigene Verwaltungsseite

### 10. Artikelverzeichnis (`/masterdata/articles`)
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
- **SQL-Konsole**: Direkte SQL-Ausführung (Entwickler-Tool) inkl. **Zeitbereich löschen**
  (generiert ein FK-sicheres Lösch-Skript für Belege/Buchungen/Rechnungen/Angebote
  eines Zeitraums ins SQL-Feld; Ausführung erst nach manueller Bestätigung)
- **Datensicherung**: Backup nach `<UserDir>/backup/JJJJMMTT_Backup.7zip`
  (7-Zip, ohne 7-Zip als `.zip`; wahlweise nur DB oder alle Daten; DB als
  konsistenter Snapshot) + **Wiederherstellung** (überschreiben oder vorher löschen)
- **SQL-Audit-Logs**: `sql_operations.log/.sql` werden ab 5 MB automatisch
  rotiert und komprimiert archiviert (`JJJJMMTT-HHMMSS_sql_operations.*`)

### 15. Angebote (`/quote`)
- Angebote als `Invoices`-Zeilen mit `DocumentType='quote'` (gleiche Positionen/PDF-Logik)
- Umwandlung Angebot → Rechnung (`SourceQuoteId` verweist auf die Quelle)
- Status-Workflow und Gültigkeitsdatum (`ValidUntil`)

### 16. Arbeitszeiten (`/worktime`)
- Zeiterfassung je Person (Arbeit/Urlaub/Krank/Feiertag) mit Pausen und Einsatzort
- Überlappungsprüfung, Kundenzuordnung
- **PDF-Stundenzettel** (`export/pdf_worktime.py`)

### 17. Fahrtenbuch (`/trips`)
- Fahrten je Fahrer mit Start/Ziel, km-Ständen und Belegverknüpfung
- km-Vorbelegung aus der letzten Fahrt

### 18. Mehrbenutzer & Benutzerverwaltung (`/users`)
- Betriebsmodus **Einzelbenutzer** (ohne Login) oder **Mehrbenutzer** (Login,
  je Nutzer isoliertes Datenverzeichnis `data/users/<user>/`) – Auswahl bei der
  Ersteinrichtung, gespeichert in `data/config.json`
- Sessions (HttpOnly, SameSite=Strict, Secure bei HTTPS), scrypt-Passwort-Hashes
- CSRF-Schutz für alle schreibenden Requests (Token an die Session gebunden)
- Admin-Funktionen: Nutzer anlegen/löschen, Passwort zurücksetzen, Admin-Flag
- Details zu LAN/HTTPS-Betrieb: siehe [DEPLOYMENT.md](DEPLOYMENT.md)

### 19. About (`/about`)
Informationen über die Anwendung (Version aus `version.py`).

---

## Projektstruktur

```
uBuchhaltung/
├── main.py                    # Entry Point – Webserver starten
├── version.py                 # Single Source of Truth für die Versionsnummer
├── money.py                   # Geld als Festkomma-Integer (Minor Units, SCALE=4)
├── auth.py                    # Benutzer/Sessions/CSRF (Mehrbenutzer-Modus)
├── userctx.py                 # Request-Kontext: Modus, Benutzer, Datenpfade
├── db/                        # Datenbank-Package (CRUD, Import, Linking, Seeding)
│   ├── __init__.py            #   Database-Klasse (Komposition aller Mixins)
│   ├── core.py                #   Verbindung, Init-Guard, Geld-Grenze, coa_id, SQL-Log
│   ├── schema.py              #   alle CREATE TABLE / Schema-Setup
│   ├── bookings.py · matching.py · invoices.py · contacts.py · …
│   └── (weitere Domänen-Mixins: assets, accounts, wiso_import, reporting,
│        worktimes, seed, numbering, articles, receipts …)
├── document_parser.py         # PDF-Parser für Kontoauszüge + SQL-Audit-Log/-Rotation
├── export/                    # Ausgabe-/Export-Generatoren (Package)
│   ├── pdf_core.py            #   Gemeinsame PDF-Primitive (Builder, Logo, Escaping)
│   ├── pdf_invoice.py         #   PDF-Rechnungs-/Angebotsgenerierung
│   ├── pdf_worktime.py        #   PDF-Stundenzettel (Arbeitszeiten)
│   ├── xrechnung_invoice.py   #   XRechnung XML (EN 16931)
│   └── datev.py               #   DATEV-Buchungsstapel-Export (CSV)
├── email_sender.py            # E-Mail-Versand (SMTP)
├── buch.css                   # Stylesheet (inkl. Dark Mode; versioniert via ?v=)
├── README.md                  # Diese Datei
├── DB_MODEL.md                # Detailliertes Datenbankmodell
├── PARSER_README.md           # Dokumentation Parser & CSV-Import
├── DEPLOYMENT.md              # LAN-/HTTPS-/Mehrbenutzer-Betrieb
├── AGENTS.md                  # Leitfaden für KI-/Coding-Agenten im Repo
├── requirements.txt           # Laufzeit-Dependencies (pdfplumber)
├── requirements-dev.txt       # zusätzlich Test-Dependencies (pytest)
├── run_https.ps1              # HTTPS-Schnellstart (selbstsigniertes Zertifikat)
├── seed_data/                 # Initialisierungsdaten (JSON)
│   ├── tax_keys.json          # 50 DATEV-Steuerschlüssel (BU-Codes)
│   ├── asset_categories.json  # 30 BMF-AfA-Kategorien
│   ├── chart_of_accounts_skr04.json  # Standard-SKR04-Kontenrahmen
│   ├── private/               # Benutzerspezifisch (in .gitignore)
│   └── test/                  # Testdaten (nur anonymisierte/erfundene Werte)
├── server/                    # Modularer Webserver
│   ├── app.py                 # HTTP-Server: Routing, Auth-Gate, CSRF, Statics
│   ├── pages.py               # Gemeinsame HTML (Header, Footer, Nav, CSRF-JS)
│   ├── pages_dashboard.py     # Dashboard + EÜR
│   ├── pages_invoice.py       # Rechnungen · pages_quote.py – Angebote
│   ├── pages_transactions.py  # Buchungen (Bank+Entry merged Display)
│   ├── pages_booking_groups.py # Split-Buchungen
│   ├── pages_contacts.py      # Kontaktverwaltung (6-Tabellen-Schema)
│   ├── pages_masterdata.py    # Stammdaten (Artikel, SKR, Bankkonten, Nummernkreise)
│   ├── pages_assets.py        # Anlagenverzeichnis
│   ├── pages_worktime.py      # Arbeitszeiten · pages_trips.py – Fahrtenbuch
│   ├── pages_setup.py         # Ersteinrichtung · pages_login.py – Login/Bootstrap
│   ├── pages_users.py         # Benutzerverwaltung (Admin)
│   ├── pages_receipts.py      # Belege (Upload, Bearbeiten)
│   ├── pages_miscellaneous.py # DB-Export, SQL-Konsole, WISO-Import, Backup
│   ├── handlers.py            # POST-Handler (Formulare, PDF, Import)
│   ├── upload_handler.py      # File-Upload mit PDF-Parsing
│   ├── import_preview.py      # Kontoauszug-Import-Vorschau (Duplikat-Zähler)
│   ├── multipart.py           # Multipart-Parser (E-Mail-Parser-basiert)
│   ├── period.py              # Zeitraum-Auswahl (Cookie-basiert)
│   └── backup.py              # Datensicherung/Wiederherstellung
├── tests/                     # pytest-Suite (nur anonymisierte Daten,
│                              #   Guard: tests/check_anonymized.py)
├── static/                    # Statische Dateien (Logos)
└── data/                      # Laufzeitdaten (in .gitignore)
    ├── config.json            # Betriebsmodus (single/multi)
    ├── buch.db                # SQLite-DB (Einzelbenutzer-Modus)
    └── users/<user>/          # je Nutzer: buch.db, Belege/, backup/, Logs …
```

> Datenbankschema: siehe [DB_MODEL.md](DB_MODEL.md)

---

## Installation und Start

### Voraussetzungen
- Python 3.10+
- Der Kern läuft mit der Standardbibliothek. Externe Abhängigkeiten (nur für
  PDF-Parsing) installieren:

```bash
pip install -r requirements.txt
```

Für Tests zusätzlich `pip install -r requirements-dev.txt` (pytest).

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

### PDF-Parser

Wird durch `pip install -r requirements.txt` (pdfplumber) automatisch
aktiviert. Optionales OCR für gescannte PDFs: in `requirements.txt` die
auskommentierten Zeilen (`pytesseract`, `pdf2image`) einkommentieren.

---

## Workflow

### Ersteinrichtung
1. `python main.py` starten → Modus wählen (Einzel-/Mehrbenutzer), DB mit Seed-Daten wird erstellt
2. Bankkonten unter `/masterdata/bankaccounts` anlegen (mit SKR-Zuordnung, z.B. SKR 1810)
3. Kontakte unter `/masterdata/contacts` erfassen
4. Ggf. eigene SKR-Konten unter `/masterdata/skr` ergänzen

### Tägliche Nutzung
1. WISO-Export importieren unter `/miscellaneous` (Bank↔Entry automatisch verknüpft)
2. Kontoauszüge als PDF hochladen unter `/transactions` (Vorschau, dann bestätigen)
3. Buchungen prüfen und ergänzen unter `/transactions`
4. Rechnungen erstellen, PDF generieren, versenden
5. Regelmäßig sichern: `/miscellaneous` → Datensicherung

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
| `server/app.py` | Routing, Auth-Gate, CSRF, statische Dateien |
| `server/pages.py` | Gemeinsame HTML (Header, Nav, Footer, CSRF-JS) |
| `server/pages_dashboard.py` | Dashboard + EÜR |
| `server/pages_invoice.py` / `pages_quote.py` | Rechnungen / Angebote |
| `server/pages_transactions.py` | Buchungen (Bank+Entry merged) |
| `server/pages_booking_groups.py` | Split-Buchungen |
| `server/pages_contacts.py` | Kontakte |
| `server/pages_masterdata.py` | Stammdaten (Artikel, SKR, Bankkonten, Nummernkreise) |
| `server/pages_assets.py` | Anlagen |
| `server/pages_worktime.py` / `pages_trips.py` | Arbeitszeiten / Fahrtenbuch |
| `server/pages_setup.py` / `pages_login.py` | Ersteinrichtung / Login |
| `server/pages_users.py` | Benutzerverwaltung (Admin) |
| `server/pages_receipts.py` | Belege (Upload, Bearbeiten) |
| `server/pages_miscellaneous.py` | DB-Export, SQL-Konsole, WISO-Import, Backup |
| `server/handlers.py` | POST-Handler |
| `server/upload_handler.py` | File-Upload |
| `server/import_preview.py` | Kontoauszug-Import-Vorschau |
| `server/backup.py` | Datensicherung/Wiederherstellung |

---

## Sicherheitshinweise

- Im **Mehrbenutzer-Modus**: Login mit scrypt-Passwort-Hashes, Sessions
  (HttpOnly, SameSite=Strict), CSRF-Token für alle schreibenden Requests,
  je Nutzer isolierte Daten; optional HTTPS (siehe [DEPLOYMENT.md](DEPLOYMENT.md))
- Im **Einzelbenutzer-Modus** gibt es keine Authentifizierung –
  nur auf `localhost` betreiben
- ⚠️ Konzipiert für lokalen bzw. LAN-Einsatz – **nicht für das offene Internet**
  (kein Rate-Limiting, SQL-Konsole für angemeldete Nutzer, einfacher HTTP-Server)

---

## Lizenz

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Dieses Projekt steht unter der **GNU Affero General Public License v3.0 oder
später (AGPL-3.0-or-later)**. Den vollständigen Lizenztext findest du in der
Datei [`LICENSE`](LICENSE).

Kurz gesagt: Du darfst die Software frei nutzen, weitergeben und verändern.
Wenn du sie veränderst und anderen über ein Netzwerk als Dienst bereitstellst
(z. B. als gehostete Anwendung), musst du den vollständigen Quellcode deiner
Version ebenfalls unter der AGPL verfügbar machen.

Beiträge (Pull Requests) unterliegen dem Contributor License Agreement –
siehe [`CONTRIBUTING.md`](CONTRIBUTING.md) und [`CLA.md`](CLA.md).

### Kommerzielle Lizenz

Wer die Software in ein proprietäres Produkt integrieren oder als gehosteten
Dienst anbieten möchte, ohne den eigenen Quellcode unter der AGPL offenlegen
zu müssen, kann eine separate **kommerzielle Lizenz** erwerben.

Anfragen bitte an: **office@unsix.com**

### Unterstützung

Wenn dir das Projekt nützt, freue ich mich über einen Kaffee:
[GitHub Sponsors](https://github.com/sponsors/NobseVomBerg) ☕

## Autor

© 2026 unsix IT Engineering – entwickelt von NobseVomBerg
