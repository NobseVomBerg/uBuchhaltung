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

### 5. Kontakte-Verwaltung (`/masterdata/contacts`)
Verwaltung von Kunden, Lieferanten und eigenen Firmendaten in einem normalisierten 3NF-Schema (4 Tabellen):
- **Entitätstypen**: Unternehmen (`company`) und Personen (`person`) mit separaten Formularmasken
- **Kontakttypen**: Kunde, Lieferant, Eigene Daten, Versicherung, Sonstiges
- **Anzeigen**: Übersicht mit kombiniertem Filter nach Typ und Entitätstyp
- **Anlegen**: Separate „Neu“-Buttons für Unternehmen und Personen
- **Bearbeiten**: Vollständiges, kontextabhängiges Bearbeitungsformular
- **Logo-Verwaltung**:
  - File-Picker für einfache Logo-Auswahl
  - Automatische Pfadkonvertierung (absolut → relativ)
  - Live-Vorschau des gewählten Logos
- **Filter**: Nach Kontakttyp und Entitätstyp filterbar

**Datenbankstruktur (normalisiert, 4 Tabellen):**
- `Contacts` – Basis: ContactType, EntityType, DisplayName, CustomerNumber, Email, Phone, Notes, Logo
- `ContactAddresses` – 1:n Adressen: AddressLine1, Street, PostalCode, City, Country
- `CompanyDetails` – 1:1 für Unternehmen: CompanyName, LegalForm, TaxID, BuyerRouteID
- `PersonDetails` – 1:1 für Personen: Salutation, Title, FirstName, LastName, DateOfBirth, CompanyContactID

### 6. Rechnungserstellung (`/invoice`)
Professionelle Rechnungserstellung mit PDF-Generierung, Multi-Company-Support und erweiterten Funktionen:

**Rechnungsformular:**
- **Multi-Company-Unterstützung**: 
  - Auswahl der eigenen Firma aus Kontakten (Typ: "Eigene Daten")
  - Dynamisches Logo pro Firma
  - Automatische Aktualisierung von Absenderzeile und Footer
  - **Snapshot-Prinzip**: Bei Speicherung werden alle Verkäufer- und Käuferdaten (inkl. Firmennamen) als Kopie in der Rechnung gespeichert
  - **SellerCompany & BuyerCompany**: Die Firmennamen sind die wichtigsten Felder und als NOT NULL definiert
- **Rechnungskopf**: Firmenlogo, Datum, Rechnungsnummer (aus Nummernkreis), Kundennummer
- **Kundenauswahl**: Dropdown mit automatischer Adressübernahme (Name + Firma)
- **Absenderzeile**: Kompakte Absenderinfo für Brieffenster
- **XRechnung-Felder**: EN 16931 konforme Felder (BuyerReference, PaymentMeans, etc.)
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

**PDF-Generierung:**
- **Separates Modul** (`pdf_generator.py`): Saubere Trennung der PDF-Logik
- **Datenbankbasiert**: PDF wird direkt aus gespeicherten Rechnungsdaten erstellt
- **Kein Download**: PDF wird nur im Dateisystem gespeichert (`data/invoices/YYYY/Rechnung_XXX.pdf`)
- **Dateisystem-Prüfung**: Überprüft physische Dateiexistenz, nicht nur Datenbank-Eintrag
- **Überschreiben-Dialog**: Warnung wenn PDF bereits existiert (mit Option zum Abbrechen)
- **Erfolgsbestätigung**: Alert zeigt Dateipfad nach erfolgreicher Generierung
- **Professionelles Layout**: A4-Format mit korrekten deutschen Umlauten und €-Zeichen
- **Logo-Einbettung**: Firmenspezifisches Logo (PNG/JPEG) wird automatisch eingebunden
- **Automatische Pfadaktualisierung**: PDFPath-Feld in Datenbank wird aktualisiert

**E-Mail-Versand:**
- Direkter E-Mail-Versand von Rechnungen aus der Rechnungsansicht
- PDF-Anhang wird automatisch mitgeschickt
- Empfängerauswahl aus Kontakten
- Anpassbare E-Mail-Nachricht
- SMTP-Konfiguration über Umgebungsvariablen

**XRechnung XML-Export:**
- EN 16931 konformer XML-Export
- Vollständige Strukturierung nach Standard
- Download-Button auf jeder Rechnung
- Automatische Generierung aller Pflichtfelder
- Kompatibel mit ZUGFeRD und XRechnung-Validatoren

**Rechnungsstatus:**
- **Entwurf** (draft): Rechnung in Bearbeitung
- **Finalisiert** (finalized): Rechnung fertiggestellt, kann nicht mehr bearbeitet werden
- **Versendet** (sent): Rechnung wurde verschickt (E-Mail/Post)
- **Bezahlt** (paid): Rechnung wurde vollständig bezahlt
- **Teilweise bezahlt** (partially_paid): Teilzahlung erfolgt
- **Überfällig** (overdue): Zahlungsfrist überschritten
- **Storniert** (cancelled): Rechnung wurde storniert
- Farbcodierung für schnelle Übersicht
- Automatische Nummernkreis-Inkrementierung nur bei Finalisierung

### 7. Dashboard (`/` und `/dashboard`)
Zentrale Übersicht über alle wichtigen Kennzahlen:
- **Finanz-Statistiken**:
  - Gesamtumsatz (finalisierte/versendete/bezahlte Rechnungen)
  - Bezahlter Umsatz
  - Offene Beträge
  - Umsatz laufendes Jahr
- **Rechnungsstatus-Verteilung**:
  - Balkendiagramm mit Anzahl pro Status
  - Farbcodierung: Entwürfe, finalisiert, versendet, bezahlt, überfällig, storniert
- **Überfällige Rechnungen**:
  - Anzahl und Gesamtbetrag
  - Direktlink zur Mahnungsübersicht
- **Monatlicher Umsatzverlauf**:
  - Balkendiagramm für laufendes Jahr
  - Monatsweise Aufschlüsselung
- **Neueste Rechnungen**: Tabelle mit den letzten 10 Rechnungen
- **Quick Actions**: Schnellzugriff auf häufige Aktionen

### 8. Mahnwesen (`/invoice/reminders`)
Automatische Mahnstufen-Verwaltung für überfällige Rechnungen:
- **3-Stufen-Mahnsystem**:
  - **Stufe 1** (1-14 Tage überfällig): Gelb - Zahlungserinnerung
  - **Stufe 2** (15-30 Tage überfällig): Orange - 1. Mahnung
  - **Stufe 3** (>30 Tage überfällig): Rot - 2. Mahnung / Inkasso
- **Fälligkeitsvorschau**: Rechnungen, die in den nächsten 7 Tagen fällig werden
- **Übersichtliche Darstellung**:
  - Rechnungsnummer, Kunde, Betrag
  - Fälligkeitsdatum und Tage überfällig
  - Offener Restbetrag
  - Farbcodierung nach Dringlichkeit
- **Direktverlinkung**: Klick auf Rechnung öffnet Detail-Ansicht
- **Automatische Berechnung**: System berechnet Überfälligkeit anhand Fälligkeitsdatum

### 9. Zahlungsverknüpfung
Verknüpfung von Rechnungen mit Bankbuchungen:
- **Zahlung zuordnen**: Aus Rechnungsansicht direkt Zahlung verknüpfen
- **Automatische Berechnung**: Restbetrag wird automatisch aktualisiert
- **Mehrfachzahlungen**: Unterstützung für Teilzahlungen
- **Status-Update**: Bei vollständiger Zahlung automatisch auf "bezahlt" setzen
- **Übersicht verknüpfter Zahlungen**: Liste aller Zahlungen einer Rechnung

### 10. Kategorien-Verwaltung (`/categories`)
Verwaltung von Buchungskategorien:
- **Hierarchische Struktur**: Parent-Child-Beziehungen
- **Flexible Kategorisierung**: Individuell erweiterbar
- **Buchungs-Zuordnung**: Kategorien können Buchungen zugewiesen werden

### 11. Artikelverzeichnis (`/articles`)
Verwaltung von Artikeln und Dienstleistungen für Rechnungen:
- **Artikelstammdaten**: Bezeichnung, Einheit, Nettopreis, Steuersatz
- **Beschreibung**: Detaillierte Artikelbeschreibung
- **Active-Flag**: Nur aktive Artikel in Rechnungen verfügbar
- **Integration**: Direkte Übernahme in Rechnungspositionen
- **Bearbeitung**: Vollständige CRUD-Funktionalität

### 12. Nummernkreise (`/masterdata/numberranges`)
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

### 13. Split-Buchungen (`/bookinggroups`)
Gruppierung mehrerer Buchungen:
- **Erstellen**: Neue Buchungsgruppen mit Beschreibung
- **Validierung**: Prüfung der Soll/Haben-Summen
- **Übersicht**: Liste aller Gruppen mit Gesamtbeträgen
- **Detail-Ansicht**: Alle Buchungen einer Gruppe mit Validierung

### 14. Sonstiges (`/miscellaneous`)
Verschiedene Hilfsfunktionen und Entwickler-Tools:
- **Datenbank-Übersicht**: Tabellenstatistiken (Anzahl Einträge pro Tabelle)
- **DB-Export**: Exportiert alle Tabelleninhalte als INSERT-Statements nach `./data/db-export.sql`
  - Kompaktes Format mit Multi-Value INSERT-Syntax
  - Direkt verwendbar in der SQL-Konsole
  - Beispiel: `INSERT INTO Table (col1, col2) VALUES (v1, v2), (v3, v4);`
- **WISO Import**: Importiert Buchungen aus WISO Mein Büro mit automatischer Format-Erkennung
  - **Original-Export**: Vollständige Buchhaltungsdaten (9 Spalten)
    - Automatisches Mapping von KONTO und GEGENKONTO auf SKR-Konten
    - BU-Schlüssel werden in Steuersätze umgewandelt
    - Duplikat-Erkennung nach Referenznummer + Datum
  - **Tabellen-Export**: Ergänzt bestehende Buchungen (6 Spalten)
    - Fügt Empfänger/Auftraggeber und Verwendungszweck hinzu
    - Sucht nach Datum + Belegnummer + Betrag
    - UPDATE statt INSERT bei bestehenden Buchungen
    - Nur leere Felder werden ergänzt (keine Überschreibung)
  - Format wird automatisch erkannt (kein manuelles Umschalten nötig)
  - Fehlerbehandlung für fehlende SKR-Konten
  - Detailliertes Ergebnis-Reporting (importiert/aktualisiert/übersprungen)
- **SQL-Konsole**: Direktausführung beliebiger SQL-Befehle (⚠️ nur für Entwickler/Administration)

### 15. Anlagenverzeichnis (`/assets`)
Vollständiges Anlagenmanagement mit gesetzeskonformer AfA-Berechnung:

**Übersicht (`/assets`):**
- Statusfilter: Aktiv / Verkauft / Abgang
- Statistik-Header: Gesamte Anschaffungskosten, Gesamtrestbuchwert, AfA gebucht/geplant im laufenden Jahr
- Tabelle: Inventarnummer, Bezeichnung, Kategorie, Anschaffungsdatum, Anschaffungskosten, Restbuchwert, AfA aktuelles Jahr (mit „gebucht/offen"-Badge), Status, Aktionen

**Anlage anlegen / bearbeiten (`/assets/new`, `/assets/edit?id=`):**
- Pflichtfelder: Bezeichnung, Anschaffungsdatum, Anschaffungskosten (netto), Nutzungsdauer
- Automatische Inventarnummer-Vergabe im Format `INV-YY-###` (z.B. `INV-26-001`)
- Kategorie-Dropdown mit automatischer Vorbefüllung von Nutzungsdauer und AfA-Methode
- AfA-Methode: Linear oder Degressiv (25% fester Satz)
- **GWG-Hinweis**: Bei Anschaffungskosten ≤ 800 € automatisch Sofortabschreibung
- **Live-AfA-Vorschau**: JavaScript-basierte Echtzeit-Berechnung des kompletten AfA-Plans im Formular
- Verknüpfung mit Lieferant (Kontakte), Beleg (Dokumente), übergeordneter Anlage (Erweiterung)
- Seriennummer, Standort, Notizen, SKR-Konto

**Detailansicht (`/assets/view?id=`):**
- Stammdaten-Block mit allen Anlageninformationen
- Vollständiger AfA-Plan (alle Jahre): Buchwert Anfang | AfA-Betrag | Buchwert Ende | Methode | Status | Aktion
- Inline-Buchungsformular je Jahr (Buchungskonto + SKR-Aufwandskonto)
- Erweiterungen/Nachkäufe (Sub-Anlagen mit eigener Abschreibung)
- Verkauf/Abgang-Formular mit Datum und Erlös

**AfA-Kategorien (`/asset_categories`):**
- BMF-konforme Liste mit 30 vordefinierten Kategorien (IT, Büro, Fahrzeuge, Maschinen, Gebäude, etc.)
- Felder: Bezeichnung, Nutzungsdauer (Jahre), AfA-Methode, Standard-SKR-Konto, Notizen
- Vollständige CRUD-Funktionalität

**AfA-Berechnungsregeln:**
- **Linear**: `Jahres-AfA = AK / Nutzungsdauer`, erstes Jahr anteilig nach Monatsmethode (`(13 - Anschaffungsmonat) / 12`)
- **Degressiv (25%)**: 25% auf Restbuchwert, automatischer Wechsel zu linear sobald lineare AfA ≥ degressive AfA
- **GWG (Geringwertige Wirtschaftsgüter)**: Anschaffungskosten ≤ 800 € netto → Sofortabschreibung im Anschaffungsjahr
- **Anteiligkeitsregel**: Im ersten Jahr wird immer monatsgenau berechnet

**Buchungsintegration (Ansatz C):**
- AfA-Buchung erzeugt Eintrag in Bookings-Tabelle (BookingType='expense', Status='posted')
- Gleichzeitig Eintrag in AssetDepreciations mit Verweis auf die Buchung
- AfA-Buchungen erscheinen automatisch in Buchungsübersicht
- Status je Jahr: „Geplant" (nur kalkuliert) → „Gebucht" (Buchung vorhanden)

**Erweiterungen / Nachkäufe:**
- Sub-Anlagen über Parent_ID-Verknüpfung
- Eigene AfA-Berechnung pro Erweiterungs-Objekt
- Anzeige in Detailansicht der Hauptanlage

### 16. About-Seite (`/about`)
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

**Contacts (Basis)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- ContactType (TEXT) - 'customer', 'supplier', 'own', 'insurance', 'other'
- EntityType (TEXT) - 'company' oder 'person'
- DisplayName (TEXT) - Anzeigename
- CustomerNumber (TEXT)
- Email (TEXT)
- Phone (TEXT)
- Notes (TEXT)
- Logo (TEXT) - Pfad zum Firmenlogo

**ContactAddresses (Adressen, 1:n)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- ContactID (INTEGER, FK → Contacts ON DELETE CASCADE)
- AddressType (TEXT DEFAULT 'main')
- AddressLine1 (TEXT) - Adresszusatz / c/o
- Street (TEXT)
- PostalCode (TEXT)
- City (TEXT)
- Country (TEXT DEFAULT 'DE')

**CompanyDetails (Unternehmensdetails, 1:1)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- ContactID (INTEGER, FK → Contacts ON DELETE CASCADE)
- CompanyName (TEXT)
- LegalForm (TEXT) - z.B. GmbH, AG, UG
- TaxID (TEXT) - Umsatzsteuer-ID / Steuernummer
- BuyerRouteID (TEXT) - XRechnung: Leitweg-ID

**PersonDetails (Personendetails, 1:1)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- ContactID (INTEGER, FK → Contacts ON DELETE CASCADE)
- Salutation (TEXT)
- Title (TEXT)
- FirstName (TEXT)
- LastName (TEXT)
- DateOfBirth (TEXT)
- CompanyContactID (INTEGER, FK → Contacts) - Zugehöriges Unternehmen
- CompanyName_Free (TEXT) - Freier Firmenname

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

**Invoice (Rechnungen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- InvoiceNumber (TEXT UNIQUE NOT NULL) - Rechnungsnummer
- InvoiceDate (DATE NOT NULL) - Rechnungsdatum
- **Verkäufer (Eigene Firma) - Snapshot:**
  - OwnCompanyId (INTEGER, FOREIGN KEY zu Contacts)
  - SellerName (TEXT NOT NULL) - Kontaktname
  - SellerCompany (TEXT NOT NULL) - **Firmenname** (wichtigstes Feld)
  - SellerStreet (TEXT) - Straße
  - SellerPostalCode (TEXT) - PLZ
  - SellerCity (TEXT) - Stadt
  - SellerCountry (TEXT DEFAULT 'DE') - Land
  - SellerVATID (TEXT) - USt-ID
  - SellerEmail (TEXT) - E-Mail
  - SellerPhone (TEXT) - Telefon
- **Käufer (Kunde) - Snapshot:**
  - CustomerId (INTEGER, FOREIGN KEY zu Contacts)
  - BuyerName (TEXT NOT NULL) - Kontaktname
  - BuyerCompany (TEXT NOT NULL) - **Firmenname** (wichtigstes Feld)
  - BuyerStreet (TEXT) - Straße
  - BuyerPostalCode (TEXT) - PLZ
  - BuyerCity (TEXT) - Stadt
  - BuyerCountry (TEXT DEFAULT 'DE') - Land
  - BuyerVATID (TEXT) - USt-ID
  - BuyerReference (TEXT) - Kundenreferenz
  - BuyerRouteID (TEXT) - Leitweg-ID (XRechnung)
- **Bestellung:**
  - OrderNumber (TEXT) - Bestellnummer
- **XRechnung:**
  - Currency (TEXT DEFAULT 'EUR') - Währung
  - DeliveryDate (DATE) - Lieferdatum
- **Zahlungsbedingungen:**
  - PaymentTerms (TEXT) - Zahlungsbedingungen-Text
  - PaymentDueDate (DATE) - Fälligkeitsdatum
  - SkontoDays (INTEGER) - Skonto-Tage
  - SkontoPercent (REAL) - Skonto-Prozentsatz
- **Bankverbindung - Snapshot:**
  - BankAccountId (INTEGER, FOREIGN KEY zu Accounts)
  - BankName (TEXT) - Bankname
  - BankIBAN (TEXT) - IBAN
  - BankBIC (TEXT) - BIC
- **Beträge:**
  - TaxCategory (TEXT DEFAULT 'S') - Steuerkategorie
  - TaxRate (REAL NOT NULL) - Steuersatz (als Dezimalzahl)
  - SumNet (REAL NOT NULL) - Nettobetrag
  - TaxAmount (REAL NOT NULL) - Steuerbetrag
  - SumGross (REAL NOT NULL) - Bruttobetrag
  - AmountDue (REAL) - Fälliger Betrag
- **Status und Dateien:**
  - Status (TEXT DEFAULT 'draft') - Status (draft/finalized/sent/paid/cancelled)
  - PDFPath (TEXT) - Pfad zur PDF-Datei
  - XMLPath (TEXT) - Pfad zur XRechnung-XML
  - CreatedAt (DATETIME DEFAULT CURRENT_TIMESTAMP)
  - UpdatedAt (DATETIME DEFAULT CURRENT_TIMESTAMP)

**InvoiceItems (Rechnungspositionen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- InvoiceID (INTEGER, FOREIGN KEY zu Invoice)
- Position (INTEGER) - Positionsnummer
- Quantity (REAL) - Menge
- Unit (TEXT) - Einheit
- Description (TEXT) - Beschreibung
- UnitPrice (REAL) - Einzelpreis (Netto)
- TotalPrice (REAL) - Gesamtpreis (Netto)
- TaxRate (REAL) - Steuersatz

**InvoicePayments (Zahlungsverknüpfungen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- InvoiceID (INTEGER, FOREIGN KEY zu Invoice)
- BookingID (INTEGER, FOREIGN KEY zu Bookings)
- Amount (REAL) - Zahlungsbetrag
- PaymentDate (DATE) - Zahlungsdatum
- Notes (TEXT) - Notizen

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
- COA_ID (INTEGER, FOREIGN KEY zu ChartOfAccounts) - SKR-Sollkonto
- CounterCOA_ID (INTEGER, FOREIGN KEY zu ChartOfAccounts) - SKR-Habenkonto/Gegenkonto (doppelte Buchführung)
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

**AssetCategories (AfA-Kategorien)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Name (TEXT NOT NULL) - Kategoriebezeichnung (z.B. "PC / Laptop", "PKW")
- UsefulLifeYears (INTEGER NOT NULL) - Standardnutzungsdauer in Jahren
- DepreciationMethod (TEXT NOT NULL) - 'linear', 'degressive' oder 'both'
- COA_ID (INTEGER, FOREIGN KEY zu ChartOfAccounts) - Standard-SKR-Konto
- Notes (TEXT) - Notizen / Quelle (z.B. "AfA-Tabelle BMF")
- 30 vordefinierte BMF-Kategorien werden beim ersten Start automatisch eingefügt

**Assets (Anlagegüter)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- InventoryNumber (TEXT UNIQUE) - Inventarnummer, auto-generiert: `INV-YY-###`
- Name (TEXT NOT NULL) - Bezeichnung
- Description (TEXT) - Beschreibung
- AssetCategory_ID (INTEGER, FOREIGN KEY zu AssetCategories)
- COA_ID (INTEGER, FOREIGN KEY zu ChartOfAccounts) - SKR-Anlagenkonto
- PurchaseDate (DATE NOT NULL) - Anschaffungsdatum
- PurchasePrice (REAL NOT NULL) - Anschaffungskosten (netto)
- UsefulLifeYears (INTEGER NOT NULL) - Nutzungsdauer in Jahren
- DepreciationMethod (TEXT NOT NULL) - 'linear', 'degressive' oder 'GWG'
- SerialNumber (TEXT) - Seriennummer
- Location (TEXT) - Standort
- Supplier_ID (INTEGER, FOREIGN KEY zu Contacts) - Lieferant
- Document_ID (INTEGER, FOREIGN KEY zu Documents) - Verknüpfter Beleg
- Booking_ID (INTEGER, FOREIGN KEY zu Bookings) - Anschaffungsbuchung
- SaleDate (DATE) - Verkaufsdatum
- SalePrice (REAL) - Verkaufserlös
- Status (TEXT DEFAULT 'active') - 'active', 'sold', 'scrapped'
- Notes (TEXT) - Notizen
- Parent_ID (INTEGER, FOREIGN KEY zu Assets) - Übergeordnete Anlage (für Erweiterungen)
- CreatedAt (DATETIME DEFAULT CURRENT_TIMESTAMP)

**AssetDepreciations (AfA-Buchungen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Asset_ID (INTEGER NOT NULL, FOREIGN KEY zu Assets)
- Year (INTEGER NOT NULL) - Abschreibungsjahr
- DepreciationAmount (REAL) - AfA-Betrag des Jahres
- BookValue (REAL) - Restbuchwert nach AfA
- Booking_ID (INTEGER, FOREIGN KEY zu Bookings) - Verknüpfte Buchung
- Status (TEXT DEFAULT 'planned') - 'planned' oder 'posted'
- BookedAt (DATETIME) - Buchungszeitpunkt
- UNIQUE(Asset_ID, Year)

### Projektstruktur

```
PyBuch/
├── main.py                    # Entry Point - Startet den Webserver
├── db.py                      # Datenbank-Layer mit allen CRUD-Operationen
│                              #   inkl. Asset-Management, AfA-Berechnung, Migrations-System
├── document_parser.py         # PDF-Parser für Kontoauszüge (VBR)
├── email_sender.py            # E-Mail-Versand mit SMTP
├── xrechnung_generator.py     # XRechnung XML-Generierung (EN 16931)
├── buch.css                   # Stylesheet für die Weboberfläche (inkl. Dark Mode)
├── README.md                  # Diese Datei
├── PARSER_README.md           # Dokumentation für den PDF-Parser
├── requirements_parser.txt    # Python-Abhängigkeiten für Parser
├── server/                    # Modularer Webserver (refactored)
│   ├── __init__.py           # Package initialization
│   ├── app.py                # HTTP-Server-Klasse mit Routing
│   ├── pages.py              # HTML-Seiten-Generierung (Dashboard, Rechnungen, Buchungen, ...)
│   ├── pages_masterdata.py   # HTML-Seiten für Stammdaten (Artikel, SKR, Bankkonten, ...)
│   ├── pages_contacts.py     # HTML-Seiten für Kontaktverwaltung (normalisiertes Schema)
│   ├── pages_assets.py       # HTML-Seiten für Anlagenverzeichnis
│   ├── pages_miscellaneous.py # HTML-Seiten für Sonstiges (DB-Export, SQL-Konsole)
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

### E-Mail-Versand konfigurieren (optional)

Für den E-Mail-Versand müssen SMTP-Zugangsdaten als Umgebungsvariablen gesetzt werden:

**Windows (PowerShell):**
```powershell
$env:SMTP_HOST = "smtp.gmail.com"
$env:SMTP_PORT = "587"
$env:SMTP_USER = "ihre-email@gmail.com"
$env:SMTP_PASSWORD = "ihr-app-passwort"
```

**Linux/Mac:**
```bash
export SMTP_HOST="smtp.gmail.com"
export SMTP_PORT="587"
export SMTP_USER="ihre-email@gmail.com"
export SMTP_PASSWORD="ihr-app-passwort"
```

**Hinweise:**
- Für Gmail: Verwenden Sie ein App-Passwort, nicht Ihr normales Passwort
- Andere Provider: Passen Sie SMTP_HOST und SMTP_PORT entsprechend an
- Umgebungsvariablen müssen vor dem Serverstart gesetzt werden

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
11. **Anlagen erfassen**: 
    - Navigieren Sie zu `/assets`
    - Legen Sie Anlagegüter unter `/assets/new` an
    - Wählen Sie eine AfA-Kategorie (Nutzungsdauer und Methode werden vorbelegt)
    - Live-Vorschau zeigt den kompletten AfA-Plan sofort an
    - Buchen Sie die jährliche AfA direkt aus der Detailansicht

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
  - `server/pages.py`: HTML-Generierung (Dashboard, Rechnungen, Buchungen, ...)
  - `server/pages_masterdata.py`: HTML-Generierung für Stammdaten (Artikel, SKR, Bankkonten, Nummernkreise)
  - `server/pages_contacts.py`: HTML-Generierung für Kontaktverwaltung (normalisiertes Schema)
  - `server/pages_assets.py`: HTML-Generierung für Anlagenverzeichnis (5 Seiten)
  - `server/pages_miscellaneous.py`: HTML-Generierung für Sonstiges (DB-Export, SQL-Konsole)
  - `server/handlers.py`: Form-Verarbeitung (POST), inkl. Asset-Handler
  - `server/upload_handler.py`: File-Upload mit Multipart-Parsing
- **Datenbank**: SQLite mit `sqlite3` Modul, Migrations-System in `db.py`
- **PDF-Parsing**: pdfplumber für VBR-Kontoauszüge
- **Frontend**: Server-seitig generiertes HTML mit JavaScript für Filter, AfA-Vorschau und Drag & Drop
- **Styling**: Externes CSS (`buch.css`)

### Hauptseiten der Anwendung
1. **Dashboard** (`/` und `/dashboard`) - Finanzübersicht mit Statistiken und Diagrammen
2. **Rechnung** (`/invoice`) - Rechnungsübersicht mit Such- und Filterfunktionen
3. **Rechnung erstellen** (`/invoice/new`) - Rechnungserstellung mit PDF-Export und Multi-Company
4. **Rechnung anzeigen** (`/invoice/view`) - Detailansicht mit E-Mail-Versand und XML-Export
5. **Mahnwesen** (`/invoice/reminders`) - Überfällige Rechnungen mit 3-Stufen-Mahnsystem
6. **Belege** (`/receipts`) - Dokumentenverwaltung mit Upload
7. **Belege bearbeiten** (`/receipts/edit`) - Detail-Ansicht mit Verknüpfungen
8. **Buchungen** (`/transactions`) - Haupt-Buchungsinterface mit Filtern
9. **Buchungen bearbeiten** (`/transactions/edit`) - Buchungs-Editor
10. **Split-Buchungen** (`/bookinggroups`) - Buchungsgruppen-Übersicht
11. **Split-Buchungen Details** (`/bookinggroups/view`) - Gruppen-Details mit Validierung
12. **Anlagen** (`/assets`) - Anlagenverzeichnis-Übersicht mit Statistiken und Statusfilter
13. **Anlage anlegen** (`/assets/new`) - Formular mit Live-AfA-Vorschau
14. **Anlage bearbeiten** (`/assets/edit?id=`) - Bearbeitungsformular
15. **Anlage Detailansicht** (`/assets/view?id=`) - AfA-Plan, Buchung, Erweiterungen, Abgang
16. **AfA-Kategorien** (`/asset_categories`) - Kategorien-Verwaltung mit 30 BMF-Vorlagen
17. **AfA-Kategorie bearbeiten** (`/asset_categories/edit?id=`) - Kategorie-Editor
18. **Import-Bestätigung** (`/confirm_transactions`) - Transaktions-Import aus PDF
19. **SKR** (`/skr`) - Standardkontenrahmen-Verwaltung
20. **SKR bearbeiten** (`/edit_skr`) - SKR-Editor
21. **Kontakte** (`/masterdata/contacts`) - Kunden/Lieferanten/Eigene Daten (normalisiert)
22. **Kontakt neu Unternehmen** (`/masterdata/contacts/new?entity=company`) - Unternehmensformular
23. **Kontakt neu Person** (`/masterdata/contacts/new?entity=person`) - Personenformular
24. **Kontakt bearbeiten** (`/masterdata/contacts/edit?id=`) - Kontakt-Editor
25. **Artikel** (`/masterdata/articles`) - Artikelverzeichnis-Verwaltung
26. **Artikel bearbeiten** (`/masterdata/articles/edit?id=`) - Artikel-Editor
27. **Bankkonten** (`/masterdata/bankaccounts`) - Bankkonten-Verwaltung
28. **Bankkonten bearbeiten** (`/masterdata/bankaccounts/edit?id=`) - Bankkonto-Editor
29. **Nummernkreise** (`/masterdata/numberranges`) - Nummernkreis-Verwaltung
30. **Nummernkreise bearbeiten** (`/masterdata/numberranges/edit?id=`) - Nummernkreis-Editor
31. **Sonstiges** (`/miscellaneous`) - DB-Übersicht, DB-Export, SQL-Konsole
32. **About** (`/about`) - Informationen

### Besondere Features

**Anlagenverzeichnis mit AfA-Berechnung**
- Vollständiges Anlagen-Management mit gesetzeskonformer Abschreibung
- **Inventarnummern** automatisch generiert: `INV-YY-###`
- **30 BMF-Kategorien** vorinstalliert (IT, Büro, Fahrzeuge, Maschinen, Gebäude, Sonstiges)
- **AfA-Methoden**:
  - Linear mit anteiliger Berechnung im ersten Jahr (Monatsmethode)
  - Degressiv (25%) mit automatischem Methodenwechsel zu linear
  - GWG-Sofortabschreibung bei Anschaffungskosten ≤ 800 € netto
- **Live-Vorschau**: JavaScript berechnet den kompletten AfA-Plan während der Eingabe
- **Buchungsintegration (Ansatz C)**: AfA-Buchung erscheint direkt in der Buchungsübersicht
- **Erweiterungen**: Sub-Anlagen (Nachkäufe) über Parent_ID-Verknüpfung
- **Datenbankbasiertes Migrations-System** (`_run_migrations()`) für Schema-Erweiterungen

**Multi-Company-Support**
- Mehrere eigene Firmendaten in Kontakten (Typ: "own")
- Auswahl der Firma bei Rechnungserstellung
- Dynamisches Logo je nach gewählter Firma
- Automatische Anpassung von Absenderzeile und Footer
- Getrennte Logos pro Firma möglich
- **Snapshot-Architektur**: Bei Rechnungserstellung werden alle Firmendaten (Verkäufer + Käufer) als Snapshot in der Rechnung gespeichert
  - **SellerCompany** und **BuyerCompany** als Pflichtfelder (NOT NULL)
  - Firmennamen sind das wichtigste Identifikationsmerkmal
  - Unveränderliche Rechnungsdaten auch bei späteren Änderungen in Kontakten
  - Historische Datenintegrität gewährleistet

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
- **Separates Modul**: `pdf_generator.py` - saubere Trennung der PDF-Logik vom Rest der Anwendung
- **Datenbankbasiert**: PDF wird direkt aus gespeicherten Rechnungsdaten erstellt, nicht aus Formulardaten
- **Dateisystem-Speicherung**: PDFs werden nur lokal gespeichert (`data/invoices/YYYY/Rechnung_XXX.pdf`)
- **Kein automatischer Download**: PDFs werden nur im Dateisystem abgelegt, kein Browser-Download
- **Dateisystem-Prüfung**: Überprüft physische Dateiexistenz, nicht nur Datenbank-Eintrag
- **Überschreiben-Dialog**: Warnung wenn PDF bereits existiert (mit Option zum Abbrechen oder Neu-Generieren)
- **Erfolgsbestätigung**: Alert zeigt vollständigen Dateipfad nach erfolgreicher Generierung
- **Professionelles A4-Layout**: Ohne externe PDF-Bibliotheken implementiert
- **Firmenspezifisches Logo**: PNG/JPEG mit automatischer Skalierung und Konvertierung
- **Deutsche Umlaute**: Korrekte Darstellung von ä, ö, ü, ß und €-Zeichen (WinAnsiEncoding)
- **Absenderzeile**: Optimiert für Standard-Brieffenster-Position
- **Dynamische Positionstabelle**: Hellblauer Tabellenkopf, automatische Zeilenberechnung
- **Automatische Summenberechnung**: MwSt und Gesamtbetrag mit korrekter Formatierung
- **Dreispaltiger Footer**: Firmendaten, Kontaktdaten, Bankverbindung
- **Multi-Company-Support**: Logo und Daten der jeweils gewählten Firma
- **Datenbank-Integration**: PDFPath-Feld wird automatisch aktualisiert

**Workflow PDF-Generierung:**
1. Klick auf "📄 PDF" in Rechnungsliste oder "Als PDF exportieren" in Rechnungsansicht
2. System prüft physische Dateiexistenz im Dateisystem
3. Falls vorhanden: Dialog "PDF-Datei existiert bereits. Möchten Sie die Datei überschreiben?"
4. Bei Bestätigung oder Neuanlage: `pdf_generator.generate_invoice_pdf(db, invoice_id)` wird aufgerufen
5. Modul lädt Rechnung und Positionen direkt aus Datenbank
6. PDF wird im Dateisystem gespeichert und PDFPath in DB aktualisiert
7. Erfolgsmeldung zeigt vollständigen Dateipfad
8. Keine Browser-Tabs, kein Download - nur lokale Datei

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
