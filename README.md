"# PyBuch

Eine einfache webbasierte Buchhaltungsanwendung für kleine Unternehmen und Selbstständige.

## Überblick

PyBuch ist eine schlanke, webbasierte Buchhaltungssoftware, die in Python entwickelt wurde. Sie ermöglicht die Verwaltung von Belegen, Konten, Buchungen und nutzt den deutschen Standardkontenrahmen (SKR).

## Features

### 1. Belege-Verwaltung (`/belege`)
Verwalten Sie Ihre Buchhaltungsbelege:
- **Anzeigen**: Übersicht aller Belege mit Nummer, Datum, Dateiname, Pfad und zusätzlichen Informationen
- **Hinzufügen**: Neue Belege mit eindeutiger Nummer erfassen
- **Bearbeiten**: Bestehende Belege aktualisieren
- **Dokument-Verknüpfung**: Belege können mit mehreren Buchungen verknüpft werden (Many-to-Many)
- **Datenspeicherung**: SQLite-Datenbank mit UNIQUE-Constraints für Belegnummer und Dateiname/Pfad-Kombinationen

### 2. Buchungen-Verwaltung (`/transactions`)
Verwalten Sie Ihre Buchungstransaktionen:
- **Erweiterte Buchungsfelder**:
  - Buchungsdatum und Steuerdatum (getrennt)
  - Kunde/Lieferant-Zuordnung
  - SKR-Kontozuordnung
  - Kategorie-Zuordnung
  - Mehrstufige Steuerberechnung (Steuersatz und Steuerbetrag)
  - Multi-Währung-Support (EUR, USD, GBP, CHF)
  - Buchungsart (Einnahme/Ausgabe)
  - Status-Management (Entwurf/Gebucht/Storniert)
- **Split-Buchungen**: Gruppierung mehrerer Buchungen mit Validierung
- **Dokument-Verknüpfung**: Buchungen mit Belegen verknüpfen (mit Beziehungstyp)
- **Erweiterte Filter**:
  - Datumsbereich mit Schnell-Buttons (aktuelles Jahr, -1, -2, -3 Jahre)
  - Status-Filter (Entwurf/Gebucht/Storniert)
  - Kunden-Filter
  - Währungs-Filter
  - Betragsbereich (Min/Max)
  - Konto-Filter (Checkboxen)

### 3. Konten-Verwaltung (`/konten`)
Verwalten Sie Ihre Bankkonten und Kasse:
- **Anzeigen**: Übersicht aller Konten mit Bezeichnung, Inhaber, IBAN, BIC und Bankname
- **Hinzufügen**: Neue Bankkonten anlegen
- **Bearbeiten**: Bestehende Konten aktualisieren
- **Löschen**: Konten entfernen (außer Kasse)
- **Kasse**: Spezielle Sonderform für Bargeldverwaltung
  - Automatisch beim ersten Start angelegt
  - Kann nicht gelöscht oder bearbeitet werden
  - Immer als erstes in der Liste angezeigt

**Kontenfelder:**
- Bezeichnung (Pflichtfeld)
- Inhaber
- IBAN
- BIC
- BankName
- Typ (Kasse/Bank)

### 4. Standardkontenrahmen (SKR) (`/skr`)
Verwaltung des Kontenrahmens nach deutschem Standard:
- **SKR 03/04**: Deutschland
- **SKR 07**: Österreich
- **Anzeigen**: Übersicht aller SKR-Einträge mit RahmenNr, Konto, Name und Gruppe
- **Hinzufügen**: Neue Konten zum Rahmen hinzufügen
- **Bearbeiten**: Bestehende Einträge aktualisieren
- **UNIQUE-Constraint**: Kombination aus RahmenNr und Kontonummer muss eindeutig sein

### 5. Kontakte-Verwaltung (`/contacts`)
Verwaltung von Kunden, Lieferanten und eigenen Firmendaten:
- **Kontakttypen**: Kunde, Lieferant, Eigene Daten, Versicherung, Sonstiges
- **Anzeigen**: Übersicht mit Name, Firma, Kontaktdaten
- **Hinzufügen**: Neue Kontakte mit vollständigen Adress- und Kontaktdaten
- **Bearbeiten**: Bestehende Einträge aktualisieren
- **Eigene Daten**: Mehrere Firmendaten für Multi-Company-Support
- **Logo-Verwaltung**:
  - File-Picker für einfache Logo-Auswahl
  - Automatische Pfadkonvertierung (absolut → relativ)
  - Live-Vorschau des gewählten Logos
- **Filter**: Nach Kontakttyp filterbar

**Kontaktfelder:**
- Typ (customer/supplier/own/insurance/other)
- Kundennummer
- Name, Firma
- Straße, PLZ, Stadt, Land
- E-Mail, Telefon
- Steuernummer/UStIdNr
- Logo (Pfad für Firmenlogo)
- Notizen

### 6. Rechnungserstellung (`/invoice`)
Professionelle Rechnungserstellung mit PDF-Export und Multi-Company-Support:
- **Multi-Company-Unterstützung**: 
  - Auswahl der eigenen Firma aus Kontakten (Typ: "Eigene Daten")
  - Dynamisches Logo pro Firma
  - Automatische Aktualisierung von Absenderzeile und Footer
- **Rechnungskopf**: Firmenlogo, Datum, Rechnungsnummer (aus Nummernkreis), Kundennummer
- **Kundenauswahl**: Dropdown mit automatischer Adressübernahme
- **Absenderzeile**: Kompakte Absenderinfo für Brieffenster
- **Positionstabelle**: 
  - Beliebig viele Positionen hinzufügen/entfernen
  - Freie Positionen oder aus Artikelverzeichnis
  - Pos., Menge, Einheit, Bezeichnung, Einzelpreis, Gesamt
  - Automatische Berechnung der Zeilensummen
- **Artikelverzeichnis-Integration**:
  - Modal zur Auswahl vordefinierter Artikel
  - Automatische Preisübernahme
  - Nur aktive Artikel werden angezeigt
- **Summenbereich**:
  - Nettosumme (automatisch berechnet)
  - Mehrwertsteuer mit einstellbarem Steuersatz
  - Gesamtbetrag (Brutto)
- **Zahlungsbedingungen**: Editierbares Textfeld
- **Bankverbindung**: Auswahl aus angelegten Bankkonten mit dynamischer Anzeige
- **Footer**: Firmendaten, Kontaktdaten, Bankdaten (dynamisch je nach gewählter Firma)

**PDF-Export:**
- Professionelles PDF-Layout (A4)
- Firmenspezifisches Logo-Einbettung (PNG/JPEG)
- Deutsche Umlaute und €-Zeichen werden korrekt dargestellt
- Anpassbare Seitenränder
- Sofortiger Download
- Automatische Nummernkreis-Inkrementierung

### 7. Kategorien-Verwaltung (`/categories`)
Verwaltung von Buchungskategorien:
- **Hierarchische Struktur**: Parent-Child-Beziehungen
- **Flexible Kategorisierung**: Individuell erweiterbar
- **Buchungs-Zuordnung**: Kategorien können Buchungen zugewiesen werden

### 8. Artikelverzeichnis (`/articles`)
Verwaltung von Artikeln und Dienstleistungen für Rechnungen:
- **Artikelstammdaten**: Bezeichnung, Einheit, Nettopreis, Steuersatz
- **Beschreibung**: Detaillierte Artikelbeschreibung
- **Active-Flag**: Nur aktive Artikel in Rechnungen verfügbar
- **Integration**: Direkte Übernahme in Rechnungspositionen
- **Bearbeitung**: Vollständige CRUD-Funktionalität

### 9. Nummernkreise (`/settings/numberranges`)
Automatische Nummerierung für Rechnungen und Belege:
- **Format**: YY[Buchstabe][Präfix]### (z.B. 26R001, 26B_A001)
- **Typen**: 
  - Ausgangsrechnungen
  - Belegnummern Firma
  - Belegnummern Kategorien
- **Komponenten**:
  - Jahr (2-stellig)
  - Buchstabe (A-Z) als Typ-Kennzeichen
  - Optionaler Präfix für Unterteilungen
  - Fortlaufende Nummer (3-stellig)
- **Automatische Inkrementierung**: Nächste Nummer wird automatisch vorgeschlagen
- **Jahreswechsel-Support**: Separate Nummernkreise pro Jahr

### 10. Split-Buchungen (`/bookinggroups`)
Gruppierung mehrerer Buchungen:
- **Erstellen**: Neue Buchungsgruppen mit Beschreibung
- **Validierung**: Prüfung der Soll/Haben-Summen
- **Übersicht**: Liste aller Gruppen mit Gesamtbeträgen
- **Detail-Ansicht**: Alle Buchungen einer Gruppe mit Validierung

### 11. Einstellungen (`/settings`)
Zentrale Konfiguration:
- **Bankkonten**: Verwaltung von Bankkonten und Kasse
- **Nummernkreise**: Konfiguration der automatischen Nummerierung
- **Erweiterbar**: Weitere Einstellungen können hinzugefügt werden

### 12. About-Seite (`/about`)
Informationen über die Anwendung.

## Technische Details

### Datenbankstruktur

Die Anwendung verwendet SQLite mit folgenden Tabellen:

**Belege**
- Nummer (TEXT, UNIQUE)
- Datum (DATE)
- Dateiname (TEXT)
- Pfad (TEXT)
- Info (TEXT)
- UNIQUE(Dateiname, Pfad)

**Konten**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Bezeichnung (TEXT NOT NULL, UNIQUE)
- Inhaber (TEXT)
- IBAN (TEXT)
- BIC (TEXT)
- BankName (TEXT)
- IstKasse (INTEGER DEFAULT 0)

**Skr (Standardkontenrahmen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- RahmenNr (INTEGER) - z.B. 03, 04 oder 07
- Konto (INTEGER)
- Name (TEXT)
- Gruppe (TEXT)
- UNIQUE(RahmenNr, Konto)

**Customers (Kontakte)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- ContactType (TEXT) - 'customer', 'supplier', 'own', 'insurance', 'other'
- CustomerNumber (TEXT)
- Name (TEXT)
- Company (TEXT)
- Street (TEXT)
- PostalCode (TEXT)
- City (TEXT)
- Country (TEXT)
- Email (TEXT)
- Phone (TEXT)
- TaxID (TEXT) - Steuernummer/UStIdNr
- Notes (TEXT)
- Logo (TEXT) - Pfad zum Firmenlogo (für Multi-Company)

**Articles (Artikelverzeichnis)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Name (TEXT NOT NULL) - Artikelbezeichnung
- Unit (TEXT) - Einheit (z.B. "Stk.", "Std.")
- PriceNet (REAL) - Nettopreis
- TaxRate (REAL) - Steuersatz (als Dezimalzahl)
- Description (TEXT) - Detailbeschreibung
- Active (INTEGER DEFAULT 1) - Aktiv-Flag (0=inaktiv, 1=aktiv)

**NumberRanges (Nummernkreise)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Type (TEXT NOT NULL) - 'invoice', 'receipt_company', 'receipt_category'
- Year (INTEGER NOT NULL) - Jahr (4-stellig)
- Letter (TEXT NOT NULL) - Buchstabe (A-Z)
- Prefix (TEXT) - Optionaler Präfix (z.B. "_A")
- CurrentNumber (INTEGER DEFAULT 0) - Letzte vergebene Nummer
- Description (TEXT) - Beschreibung
- UNIQUE(Type, Year, Letter, Prefix)

**Categories (Buchungskategorien)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Name (TEXT NOT NULL)
- Description (TEXT)
- Parent_ID (INTEGER, FOREIGN KEY zu Categories)

**ChartOfAccounts (Kontenplan)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- AccountNumber (TEXT NOT NULL UNIQUE)
- AccountName (TEXT NOT NULL)
- AccountType (TEXT) - z.B. Asset, Liability, Revenue, Expense
- ParentAccount_ID (INTEGER, FOREIGN KEY zu ChartOfAccounts)

**BookingGroups (Split-Buchungen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Description (TEXT)
- CreatedDate (DATE)
- TotalAmount (REAL) - Erwarteter Gesamtbetrag

**Bookings (Buchungstransaktionen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- DateBooking (DATE NOT NULL) - Buchungsdatum
- DateTax (DATE) - Steuerdatum (kann abweichen)
- BookingGroup_ID (INTEGER, FOREIGN KEY zu BookingGroups) - Für Split-Buchungen
- Account_ID (INTEGER, FOREIGN KEY zu Konten) - Eigenes Konto
- ForeignBankAccount (TEXT) - Fremdes Konto (IBAN oder Name)
- RecipientClient (TEXT) - Empfänger/Auftraggeber
- Contact_id (INTEGER, FOREIGN KEY zu Customers) - Kunde/Lieferant
- COA_ID (INTEGER, FOREIGN KEY zu ChartOfAccounts) - SKR-Konto
- Category_ID (INTEGER, FOREIGN KEY zu Categories) - Kategorie
- Amount (REAL NOT NULL) - Betrag
- Currency (TEXT DEFAULT 'EUR') - Währung
- TaxRate (REAL) - Steuersatz (als Dezimalzahl, z.B. 0.19 für 19%)
- TaxAmount (REAL) - Berechneter Steuerbetrag
- Text (TEXT) - Verwendungszweck/Beschreibung
- DocumentNumber (TEXT) - Belegnummer
- BookingType (TEXT) - 'income' oder 'expense'
- Status (TEXT DEFAULT 'draft') - 'draft', 'posted' oder 'cancelled'

**BookingDocuments (Many-to-Many Verknüpfung)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Booking_ID (INTEGER, FOREIGN KEY zu Bookings)
- Document_ID (INTEGER, FOREIGN KEY zu Belege)
- RelationType (TEXT) - z.B. 'invoice', 'receipt', 'contract'
- UNIQUE(Booking_ID, Document_ID)

### Projektstruktur

```
PyBuch/
├── main.py                    # Entry Point - Startet den Webserver
├── db.py                      # Datenbank-Layer mit allen CRUD-Operationen
├── document_parser.py         # PDF-Parser für Kontoauszüge (VBR)
├── buch.css                   # Stylesheet für die Weboberfläche (inkl. Dark Mode)
├── README.md                  # Diese Datei
├── PARSER_README.md           # Dokumentation für den PDF-Parser
├── requirements_parser.txt    # Python-Abhängigkeiten für Parser
├── server/                    # Modularer Webserver (refactored)
│   ├── __init__.py           # Package initialization
│   ├── app.py                # HTTP-Server-Klasse mit Routing
│   ├── pages.py              # HTML-Seiten-Generierung (20+ Seiten)
│   ├── handlers.py           # POST-Request-Handler inkl. PDF-Generierung
│   └── upload_handler.py     # File-Upload mit PDF-Parsing
├── static/                    # Statische Dateien
│   └── *.png                 # Firmenlogos für Rechnungen (mehrere möglich)
└── data/                      # Daten-Verzeichnis (im .gitignore)
    ├── buch.db               # SQLite-Datenbank (automatisch erstellt)
    ├── Belege/               # Hochgeladene Belege
    │   └── 2025/
    │       └── Konten/
    │           └── VBR/      # Organisierte Bank-Statements
    └── pending_imports/      # Temp. Transaktionen vor Bestätigung
```

## Installation und Start

### Voraussetzungen
- Python 3.x
- Pillow (für PDF-Logo-Einbettung): `pip install Pillow`
- Keine weiteren externen Abhängigkeiten (nutzt nur Python Standard Library)

### Server starten

```bash
python main.py
```

Der Server startet standardmäßig auf `http://localhost:8080`

### Alternative Konfiguration

```python
# In server/app.py die run_server() Funktion anpassen:
run_server(host="0.0.0.0", port=8000)  # Für anderen Host/Port
```

### PDF-Parser aktivieren (optional)

```bash
pip install -r requirements_parser.txt
```

Damit werden VBR-Kontoauszüge automatisch geparst und Transaktionen importiert.

## Verwendung

### Grundlegender Workflow

1. **Server starten**: `python main.py`
2. **Browser öffnen**: Navigieren Sie zu `http://localhost:8080`
3. **Initialisierung**: Klicken Sie auf "Initialize DB Content" um Testdaten zu laden (optional)
4. **Konten einrichten**: Erfassen Sie Ihre Bankkonten unter `/konten`
5. **Kunden/Lieferanten**: Legen Sie Geschäftspartner unter `/customers` an (optional)
6. **Belege hochladen**: 
   - Navigieren Sie zu `/belege`
   - Laden Sie PDF-Kontoauszüge hoch (Drag & Drop oder Dateiauswahl)
   - Bei VBR-Kontoauszügen werden Transaktionen automatisch erkannt
7. **Transaktionen bestätigen**: 
   - Prüfen Sie erkannte Transaktionen auf `/confirm_transactions`
   - Bestätigen Sie den Import oder korrigieren Sie Fehler
8. **Buchungen bearbeiten**:
   - Navigieren Sie zu `/transactions`
   - Nutzen Sie Filter um relevante Buchungen zu finden
   - Bearbeiten Sie Buchungen und ergänzen Sie:
     - Kunden/Lieferanten
     - SKR-Kontenzuordnung
     - Kategorien
     - Steuerberechnung
     - Dokument-Verknüpfungen
9. **Split-Buchungen**: 
   - Erstellen Sie Buchungsgruppen unter `/bookinggroups`
   - Verknüpfen Sie zusammengehörige Buchungen
   - System validiert automatisch Soll/Haben-Ausgleich
10. **Status setzen**: Ändern Sie Buchungsstatus von "Entwurf" auf "Gebucht" wenn finalisiert

### Tägliche Nutzung

1. **Neue Belege hochladen** unter `/belege`
2. **Import bestätigen** und prüfen
3. **Buchungen ergänzen** mit Zusatzinformationen (Kunde, SKR, Kategorie)
4. **Filter nutzen** um spezifische Buchungen zu finden
5. **Berichte erstellen** durch Filterung nach Datum, Kunde, Kategorie etc.

## Sicherheitshinweise

Diese Anwendung ist für lokale Verwendung konzipiert:
- ⚠️ Keine Benutzer-Authentifizierung
- ⚠️ Keine Verschlüsselung
- ⚠️ Keine Input-Validierung gegen SQL-Injection (verwendet parametrisierte Queries)
- ⚠️ Nicht für den produktiven Einsatz im Internet geeignet

## Entwicklung

### Architektur
- **Webserver**: Python's `http.server.BaseHTTPRequestHandler`
- **Modularer Aufbau**: Separation of Concerns
  - `server/app.py`: Routing und HTTP-Handler
  - `server/pages.py`: HTML-Generierung (13+ Seiten)
  - `server/handlers.py`: Form-Verarbeitung (POST)
  - `server/upload_handler.py`: File-Upload mit Multipart-Parsing
- **Datenbank**: SQLite mit `sqlite3` Modul
- **PDF-Parsing**: pdfplumber für VBR-Kontoauszüge
- **Frontend**: Server-seitig generiertes HTML mit JavaScript für Filter und Drag & Drop
- **Styling**: Externes CSS (`buch.css`)

### Hauptseiten der Anwendung
1. **Dashboard** (`/`) - Übersicht und Initialisierung
2. **Rechnung** (`/invoice`) - Rechnungserstellung mit PDF-Export und Multi-Company
3. **Belege** (`/receipts`) - Dokumentenverwaltung mit Upload
4. **Belege bearbeiten** (`/receipts/edit`) - Detail-Ansicht mit Verknüpfungen
5. **Buchungen** (`/transactions`) - Haupt-Buchungsinterface mit Filtern
6. **Buchungen bearbeiten** (`/transactions/edit`) - Buchungs-Editor
7. **Split-Buchungen** (`/bookinggroups`) - Buchungsgruppen-Übersicht
8. **Split-Buchungen Details** (`/bookinggroups/view`) - Gruppen-Details mit Validierung
9. **Import-Bestätigung** (`/confirm_transactions`) - Transaktions-Import aus PDF
10. **SKR** (`/skr`) - Standardkontenrahmen-Verwaltung
11. **SKR bearbeiten** (`/edit_skr`) - SKR-Editor
12. **Kontakte** (`/contacts`) - Kunden/Lieferanten/Eigene Daten mit Logo
13. **Kontakte bearbeiten** (`/contacts/edit`) - Kontakt-Editor mit File-Picker
14. **Artikel** (`/articles`) - Artikelverzeichnis-Verwaltung
15. **Artikel bearbeiten** (`/articles/edit`) - Artikel-Editor
16. **Einstellungen** (`/settings`) - Hauptmenü für Konfiguration
17. **Bankkonten** (`/settings/bankaccounts`) - Bankkonten-Verwaltung
18. **Bankkonten bearbeiten** (`/settings/bankaccounts/edit`) - Bankkonto-Editor
19. **Nummernkreise** (`/settings/numberranges`) - Nummernkreis-Verwaltung
20. **Nummernkreise bearbeiten** (`/settings/numberranges/edit`) - Nummernkreis-Editor
21. **About** (`/about`) - Informationen

### Besondere Features

**Multi-Company-Support**
- Mehrere eigene Firmendaten in Kontakten (Typ: "own")
- Auswahl der Firma bei Rechnungserstellung
- Dynamisches Logo je nach gewählter Firma
- Automatische Anpassung von Absenderzeile und Footer
- Getrennte Logos pro Firma möglich

**Artikelverzeichnis**
- Vordefinierte Artikel und Dienstleistungen
- Nettopreis und Steuersatz pro Artikel
- Active-Flag zur Deaktivierung ohne Löschen
- Direkte Integration in Rechnungserstellung
- Modal-Dialog zur Artikelauswahl

**Automatische Nummerierung**
- Nummernkreise für Rechnungen und Belege
- Flexibles Format: YY[Buchstabe][Präfix]###
- Jahresbasierte Nummerierung
- Automatische Inkrementierung
- Mehrere Nummernkreise parallel möglich

**Multi-Währung-Support**
- Unterstützung für EUR (Standard), USD, GBP, CHF
- Währungsauswahl bei jeder Buchung
- Filterung nach Währung möglich

**Steuerberechnung**
- Steuersatz als Prozentsatz eingeben (z.B. 19 für 19%)
- Automatische Berechnung des Steuerbetrags
- Separates Steuerdatum (kann vom Buchungsdatum abweichen)

**Status-Management**
- **Entwurf** (draft): Buchung in Bearbeitung
- **Gebucht** (posted): Finalisierte Buchung
- **Storniert** (cancelled): Stornierte Buchung
- Farbcodierung in der Übersicht für schnelle Übersicht

**Split-Buchungen**
- Gruppierung mehrerer zusammengehöriger Buchungen
- Automatische Validierung (Soll = Haben)
- Beschreibung und erwarteter Gesamtbetrag
- Detail-Ansicht mit allen Mitgliedsbuchungen

**Dokument-Verknüpfung**
- Many-to-Many-Beziehung zwischen Belegen und Buchungen
- Beziehungstypen (invoice, receipt, contract, etc.)
- Bidirektionale Ansicht (Beleg → Buchungen, Buchung → Belege)
- Einfaches Verknüpfen/Entfernen in der UI

**Rechnungs-PDF-Generierung**
- Professionelles A4-Layout ohne externe PDF-Bibliotheken
- Firmenspezifisches Logo (PNG/JPEG) mit automatischer Skalierung
- Deutsche Umlaute (ä, ö, ü, ß) und €-Zeichen
- Absenderzeile für Brieffenster-Position
- Dynamische Positionstabelle mit hellblauem Tabellenkopf
- Automatische Summen- und MwSt-Berechnung
- Dreispaltiger Footer mit Firmendaten, Kontakt, Bankverbindung
- Konfigurierbare Seitenränder
- Multi-Company-Support: Logo und Daten der gewählten Firma

**User Interface**
- Dark Mode-Unterstützung (CSS-Optimierungen)
- File-Picker mit Live-Vorschau für Logos
- Drag & Drop für PDF-Upload
- Modal-Dialoge für Artikelauswahl
- Responsive Layout
- JavaScript-basierte Filter ohne Page-Reload

**Erweiterte Filterung**
- Datumsbereich mit Jahr-Schnell-Buttons
- Status-Filter (Entwurf/Gebucht/Storniert)
- Kunden-Filter
- Währungs-Filter
- Betragsbereich (Min/Max)
- Konto-Filter (Multiple Selection via Checkboxen)
- Alle Filter kombinierbar

### Erweiterungen
Die modulare Struktur erlaubt einfache Erweiterungen:
- **Neue Seiten**: Fügen Sie Funktionen in `server/pages.py` hinzu (z.B. `PageXXX()`)
- **Neue Routes**: Erweitern Sie `do_GET()` in `server/app.py`
- **Neue Handler**: Fügen Sie Funktionen in `server/handlers.py` hinzu
- **Neue Tabellen**: Erweitern Sie `db.py` mit entsprechenden CRUD-Methoden
- **Neue Parser**: Erweitern Sie `document_parser.py` für weitere Banken

### Vorteile der Modularisierung
- ✅ Kleinere Dateien (~150-750 Zeilen statt 1048)
- ✅ Klare Trennung der Verantwortlichkeiten
- ✅ Bessere KI/Copilot-Unterstützung (vollständiger Kontext)
- ✅ Einfachere Wartung und Debugging
- ✅ Parallel-Entwicklung möglich

## Lizenz

Dieses Projekt ist für Lern- und Demonstrationszwecke erstellt.

## Autor

NobseVomBerg
" 
