# Datenbankmodell PyBuch - AI-Readable Database Schema

**Status:** Aktuell (Stand: 17. März 2026)  
**DBMS:** SQLite 3  
**File:** `data/buch.db`

---

## Übersicht

Das PyBuch-System verwaltet eine doppelte Buchführung für kleine bis mittlere Unternehmen mit Fokus auf deutsche/österreichische Buchhaltungsstandards (SKR03/SKR04/SKR07). Es unterstützt XRechnung-konforme Rechnungserstellung, Anlagenverwaltung (AfA) und Belegmanagement.

---

## Tabellen-Übersicht

```
┌─────────────────────┐
│ STAMMDATEN          │
├─────────────────────┤
│ • ChartOfAccounts   │ Kontenrahmen (SKR03/04/07)
│ • Accounts          │ Bankkonten/Kasse
│ • Contacts          │ Kunden/Lieferanten/Eigene Firma (Basis)
│ • ContactAddresses  │ Adressen zu Kontakten (1:n)
│ • CompanyDetails    │ Unternehmensdetails (1:1)
│ • PersonDetails     │ Personendetails (1:1)
│ • Articles          │ Artikel-/Dienstleistungskatalog
│ • Categories        │ Kategorien für Privatbelege
│ • AssetCategories   │ AfA-Kategorien nach BMF
└─────────────────────┘

┌─────────────────────┐
│ GESCHÄFTSVORFÄLLE   │
├─────────────────────┤
│ • Bookings          │ Buchungssätze
│ • BookingGroups     │ Splitbuchungen-Gruppen
│ • Documents         │ Belege/Dokumente
│ • BookingDocuments  │ n:m Verknüpfung Buchungen↔Belege
└─────────────────────┘

┌─────────────────────┐
│ RECHNUNGSWESEN      │
├─────────────────────┤
│ • NumberRanges      │ Nummernkreise
│ • Invoices          │ Ausgangsrechnungen (XRechnung)
│ • InvoiceItems      │ Rechnungspositionen
└─────────────────────┘

┌─────────────────────┐
│ ANLAGENVERWALTUNG   │
├─────────────────────┤
│ • Assets            │ Anlageverzeichnis
│ • AssetDepreciations│ AfA-Pläne
└─────────────────────┘
```

---

## Detaillierte Tabellendefinitionen

### 1. ChartOfAccounts (Kontenrahmen)

**Zweck:** Speichert Standard-Kontenrahmen (SKR03/04 für DE, SKR07 für AT)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Framework | INTEGER | UNIQUE(Framework, AccountNumber) | Kontenrahmen (3=SKR03, 4=SKR04, 7=SKR07) |
| AccountNumber | INTEGER | UNIQUE(Framework, AccountNumber) | Kontonummer |
| Name | TEXT | | Kontobezeichnung |
| Description | TEXT | | Zusätzliche Beschreibung |
| IsStandard | INTEGER | DEFAULT 0 | Flag: Standard-Konto |

**Indizes:** UNIQUE(Framework, AccountNumber)  
**Foreign Keys:** Keine

---

### 2. Accounts (Bank-/Kassenkonten)

**Zweck:** Verwaltung der eigenen Bankkonten und Kasse

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Name | TEXT | NOT NULL, UNIQUE | Kontoname (z.B. "Geschäftskonto", "Kasse") |
| Owner | TEXT | | Kontoinhaber |
| Number | TEXT | | IBAN oder Kreditkartennummer |
| BIC | TEXT | | BIC/SWIFT-Code |
| BankName | TEXT | | Name der Bank |
| IsCash | INTEGER | DEFAULT 0 | 1=Kasse, 0=Bankkonto |

**Indizes:** UNIQUE(Name)  
**Foreign Keys:** Keine  
**Business Logic:** Konto "Kasse" wird automatisch beim ersten Start erstellt

---

### 3. Contacts – normalisiertes Schema (4 Tabellen)

**Zweck:** Zentrale Kontaktverwaltung in 3NF. Unternehmen und Personen werden über `EntityType` unterschieden und haben je eine eigene Erweiterungstabelle.

#### 3a. Contacts (Basis)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| ContactType | TEXT | DEFAULT 'customer' | 'customer', 'supplier', 'own', 'insurance', 'other' |
| EntityType | TEXT | DEFAULT 'company' | 'company' oder 'person' |
| DisplayName | TEXT | | Anzeigename (manuell oder berechnet) |
| CustomerNumber | TEXT | UNIQUE | Kundennummer |
| Email | TEXT | | E-Mail-Adresse |
| Phone | TEXT | | Telefonnummer |
| Notes | TEXT | | Notizen |
| Logo | TEXT | | Pfad zum Firmenlogo |

**Business Logic:**
- ContactType='own' für eigene Firmendaten in Rechnungen
- EntityType steuert, welche Erweiterungstabelle befüllt wird

---

#### 3b. ContactAddresses (Adressen, 1:n)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| ContactID | INTEGER | NOT NULL, FK | → Contacts(ID) ON DELETE CASCADE |
| AddressType | TEXT | DEFAULT 'main' | 'main', 'billing', 'delivery' |
| AddressLine1 | TEXT | | Adresszusatz / c/o |
| Street | TEXT | | Straße + Hausnummer |
| PostalCode | TEXT | | PLZ |
| City | TEXT | | Ort |
| Country | TEXT | DEFAULT 'DE' | ISO-Ländercode |

---

#### 3c. CompanyDetails (Unternehmensdetails, 1:1)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| ContactID | INTEGER | UNIQUE, FK | → Contacts(ID) ON DELETE CASCADE |
| CompanyName | TEXT | | Firmenname |
| LegalForm | TEXT | | Rechtsform (GmbH, AG, UG, ...) |
| TaxID | TEXT | | Umsatzsteuer-ID / Steuernummer |
| BuyerRouteID | TEXT | | XRechnung: Leitweg-ID |

**Business Logic:** BuyerRouteID Pflichtfeld für XRechnung an öffentliche Auftraggeber

---

#### 3d. PersonDetails (Personendetails, 1:1)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| ContactID | INTEGER | UNIQUE, FK | → Contacts(ID) ON DELETE CASCADE |
| Salutation | TEXT | | Anrede (Herr/Frau/Divers) |
| Title | TEXT | | Titel (Dr., Prof.) |
| FirstName | TEXT | | Vorname |
| LastName | TEXT | | Nachname |
| DateOfBirth | TEXT | | Geburtsdatum (ISO 8601) |
| CompanyContactID | INTEGER | FK | → Contacts(ID) – zugehöriges Unternehmen |
| CompanyName_Free | TEXT | | Freier Firmenname (ohne verknüpften Kontakt) |

**Foreign Keys:** ContactID → Contacts(ID) ON DELETE CASCADE, CompanyContactID → Contacts(ID)

---

### 4. Articles (Artikelstamm)

**Zweck:** Produkt-/Dienstleistungskatalog für Rechnungen

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Name | TEXT | NOT NULL | Artikelbezeichnung |
| Unit | TEXT | DEFAULT 'Stk.' | Einheit (UN/ECE Rec. 20: C62=Stück, HUR=Stunde) |
| UnitPrice | REAL | DEFAULT 0 | Netto-Einzelpreis |
| TaxRate | REAL | DEFAULT 19 | Steuersatz in % |
| Description | TEXT | | Ausführliche Beschreibung |
| Active | INTEGER | DEFAULT 1 | 1=aktiv, 0=inaktiv |

**Indizes:** Keine  
**Foreign Keys:** Keine

---

### 5. Categories (Kategorien)

**Zweck:** Hierarchische Kategorien für private Belege (Immobilien, Versicherungen, etc.)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Name | TEXT | | Kategoriename |
| Text | TEXT | | Zusatzinfos (z.B. Steuererklärungsreferenz) |
| Parent_ID | INTEGER | | FK zu übergeordneter Kategorie (Hierarchie) |

**Indizes:** Keine  
**Foreign Keys:** Parent_ID → Categories(ID) (implizit)

---

### 6. Documents (Belege)

**Zweck:** Verwaltung von gescannten/importierten Dokumenten

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Number | TEXT | | Belegnummer |
| Date | DATE | | Belegdatum |
| Filename | TEXT | | Dateiname |
| Path | TEXT | | Dateipfad |
| Info | TEXT | | Zusatzinformationen |

**Indizes:** Keine  
**Foreign Keys:** Keine  
**Business Logic:** Kann über BookingDocuments mit mehreren Buchungen verknüpft werden

---

### 7. BookingGroups (Buchungsgruppen)

**Zweck:** Gruppierung von Splitbuchungen (z.B. eine Rechnung mit mehreren SKR-Konten)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Description | TEXT | | Beschreibung der Gruppe |
| CreatedDate | DATE | | Erstellungsdatum |
| TotalAmount | REAL | | Gesamtbetrag zur Validierung |

**Indizes:** Keine  
**Foreign Keys:** Keine

---

### 8. Bookings (Buchungssätze)

**Zweck:** Zentrale Tabelle für alle Buchungssätze (erweiterte Zahlung)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| DateBooking | DATE | NOT NULL | Buchungsdatum |
| DateTax | DATE | | Steuerdatum (optional) |
| BookingGroup_ID | INTEGER | FK | Zuordnung zu Splitbuchung |
| Account_ID | INTEGER | FK | Eigenes Bank-/Kassenkonto |
| ForeignBankAccount | TEXT | | Fremde IBAN/Kontonummer |
| RecipientClient | TEXT | | Name Empfänger/Zahler |
| Contact_ID | INTEGER | FK | Zuordnung zu Kontakt |
| COA_ID | INTEGER | FK | SKR-Kontonummer |
| Category_ID | INTEGER | FK | Kategorie (für Privatbelege) |
| Amount | REAL | NOT NULL | Betrag (positiv=Haben, negativ=Soll) |
| Currency | TEXT | DEFAULT 'EUR' | Währung |
| TaxRate | REAL | | Steuersatz (0.19 = 19%) |
| TaxAmount | REAL | | Berechneter Steuerbetrag |
| Text | TEXT | | Verwendungszweck/Notiz |
| DocumentNumber | TEXT | | Externe Belegnummer |

**Foreign Keys:**
- BookingGroup_ID → BookingGroups(ID)
- Account_ID → Accounts(ID)
- Contact_ID → Contacts(ID)
- COA_ID → ChartOfAccounts(ID)
- Category_ID → Categories(ID)

**Indizes:** Keine expliziten  
**Business Logic:**
- Amount-Vorzeichen: positiv = Einnahme (Haben), negativ = Ausgabe (Soll)
- Kann über BookingDocuments mit Documents verknüpft werden

---

### 9. BookingDocuments (Junction Table)

**Zweck:** Many-to-Many Beziehung zwischen Bookings und Documents

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Booking_ID | INTEGER | NOT NULL, FK | Buchung |
| Document_ID | INTEGER | NOT NULL, FK | Beleg |
| RelationType | TEXT | | Art der Beziehung (optional) |

**Indizes:** UNIQUE(Booking_ID, Document_ID)  
**Foreign Keys:**
- Booking_ID → Bookings(ID)
- Document_ID → Documents(ID)

---

### 10. NumberRanges (Nummernkreise)

**Zweck:** Verwaltung fortlaufender Nummern für Rechnungen und Belege

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Type | TEXT | NOT NULL | 'invoice', 'receipt_company', 'receipt_category' |
| Year | INTEGER | NOT NULL | Jahr (4-stellig) |
| Letter | TEXT | NOT NULL | Buchstabe (z.B. 'R' für Rechnung) |
| Prefix | TEXT | DEFAULT '' | Zusätzlicher Präfix |
| CurrentNumber | INTEGER | DEFAULT 0 | Letzte vergebene Nummer |
| Description | TEXT | | Beschreibung |

**Indizes:** UNIQUE(Type, Year, Letter, Prefix)  
**Foreign Keys:** Keine  
**Business Logic:** Format: `YYYY` + `Letter` + `CurrentNumber` → z.B. "26R001"

---

### 11. Invoices (Ausgangsrechnungen)

**Zweck:** XRechnung-konforme Rechnungsverwaltung

| Spalte | Typ | Constraints | Beschreibung | Index-Position* |
|--------|-----|-------------|--------------|-----------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID | 0 |
| InvoiceNumber | TEXT | UNIQUE, NOT NULL | Rechnungsnummer | 1 |
| InvoiceDate | DATE | NOT NULL | Rechnungsdatum | 2 |
| **Verkäufer (Snapshot)** | | | | |
| OwnCompanyId | INTEGER | FK | Referenz auf Contact | 3 |
| SellerName | TEXT | NOT NULL | Name | 4 |
| SellerCompany | TEXT | NOT NULL | Firma | 5 |
| SellerStreet | TEXT | | Straße | 6 |
| SellerPostalCode | TEXT | | PLZ | 7 |
| SellerCity | TEXT | | Ort | 8 |
| SellerCountry | TEXT | DEFAULT 'DE' | Land | 9 |
| SellerVATID | TEXT | | USt-ID | 10 |
| SellerEmail | TEXT | | E-Mail | 11 |
| SellerPhone | TEXT | | Telefon | 12 |
| **Käufer (Snapshot)** | | | | |
| CustomerId | INTEGER | FK | Referenz auf Contact | 13 |
| BuyerName | TEXT | NOT NULL | Name | 14 |
| BuyerCompany | TEXT | NOT NULL | Firma | 15 |
| BuyerStreet | TEXT | | Straße | 16 |
| BuyerPostalCode | TEXT | | PLZ | 17 |
| BuyerCity | TEXT | | Ort | 18 |
| BuyerCountry | TEXT | DEFAULT 'DE' | Land | 19 |
| BuyerVATID | TEXT | | USt-ID | 20 |
| BuyerReference | TEXT | | Kundenreferenz | 21 |
| BuyerRouteID | TEXT | | Leitweg-ID (XRechnung) | 22 |
| **Auftrag** | | | | |
| OrderNumber | TEXT | | Bestellnummer | 23 |
| **XRechnung** | | | | |
| Currency | TEXT | DEFAULT 'EUR' | Währung | 24 |
| DeliveryDate | DATE | | Lieferdatum | 25 |
| **Zahlungsbedingungen** | | | | |
| PaymentTerms | TEXT | | Zahlungskonditionen | 26 |
| PaymentDueDate | DATE | | Fälligkeitsdatum | 27 |
| SkontoDays | INTEGER | | Skontotage | 28 |
| SkontoPercent | REAL | | Skonto-Prozentsatz | 29 |
| **Bankverbindung (Snapshot)** | | | | |
| BankAccountId | INTEGER | FK | Referenz auf Account | 30 |
| BankName | TEXT | | Bankname | 31 |
| BankIBAN | TEXT | | IBAN | 32 |
| BankBIC | TEXT | | BIC | 33 |
| **Summen** | | | | |
| TaxCategory | TEXT | DEFAULT 'S' | Steuerkategorie (S=Standard) | 34 |
| TaxRate | REAL | NOT NULL | Steuersatz (0.19 = 19%) | 35 |
| SumNet | REAL | NOT NULL | Nettosumme | 36 |
| TaxAmount | REAL | NOT NULL | Steuerbetrag | 37 |
| SumGross | REAL | NOT NULL | Bruttosumme | 38 |
| AmountDue | REAL | NOT NULL | Fälliger Betrag | 39 |
| **Verwaltung** | | | | |
| Status | TEXT | DEFAULT 'draft' | 'draft', 'finalized', 'sent', 'paid' | 40 |
| PDFPath | TEXT | | Pfad zur generierten PDF | 41 |
| XMLPath | TEXT | | Pfad zur XRechnung-XML | 42 |
| CreatedAt | DATETIME | DEFAULT NOW | Erstellungszeitpunkt | 43 |
| UpdatedAt | DATETIME | | Letzte Änderung | 44 |

**Foreign Keys:**
- OwnCompanyId → Contacts(ID)
- CustomerId → Contacts(ID)
- BankAccountId → Accounts(ID)

**Indizes:** UNIQUE(InvoiceNumber)  
**Business Logic:**
- Snapshot-Prinzip: Alle Adressen/Bankdaten werden zum Rechnungszeitpunkt kopiert
- Status-Workflow: draft → finalized → sent → paid
- PDFPath: `data/invoices/YYYY/Rechnung_XXXXX.pdf`

\* *Index-Position in SELECT * Abfragen (0-basiert)*

---

### 12. InvoiceItems (Rechnungspositionen)

**Zweck:** Einzelne Positionen einer Rechnung

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| InvoiceId | INTEGER | NOT NULL, FK | Zugehörige Rechnung |
| Position | INTEGER | NOT NULL | Positionsnummer |
| ArticleId | INTEGER | FK | Referenz auf Artikel (optional) |
| Description | TEXT | NOT NULL | Leistungsbeschreibung |
| Quantity | REAL | NOT NULL | Menge |
| Unit | TEXT | DEFAULT 'C62' | UN/ECE Einheitencode (C62=Stück) |
| PricePerUnit | REAL | NOT NULL | Netto-Einzelpreis |
| TotalNet | REAL | NOT NULL | Netto-Zeilensumme |
| TaxCategory | TEXT | DEFAULT 'S' | Steuerkategorie |
| TaxRate | REAL | NOT NULL | Steuersatz |

**Foreign Keys:**
- InvoiceId → Invoices(ID) ON DELETE CASCADE
- ArticleId → Articles(ID)

**Indizes:** Keine  
**Business Logic:** TotalNet = Quantity × PricePerUnit

---

### 13. AssetCategories (AfA-Kategorien)

**Zweck:** AfA-Tabellen nach BMF für Anlagegüter

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Name | TEXT | NOT NULL | Kategoriename |
| UsefulLifeYears | INTEGER | NOT NULL | Nutzungsdauer in Jahren |
| DepreciationMethod | TEXT | DEFAULT 'linear' | 'linear', 'declining' |
| COA_ID | INTEGER | FK | Zugeordnetes SKR-Konto |
| Notes | TEXT | | Notizen |

**Foreign Keys:** COA_ID → ChartOfAccounts(ID)  
**Indizes:** Keine

---

### 14. Assets (Anlagenverzeichnis)

**Zweck:** Verwaltung von Anlagegütern (Anlagenbuchhaltung)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| InventoryNumber | TEXT | UNIQUE | Inventarnummer |
| Name | TEXT | NOT NULL | Anlagenbezeichnung |
| Description | TEXT | | Beschreibung |
| AssetCategory_ID | INTEGER | FK | AfA-Kategorie |
| COA_ID | INTEGER | FK | SKR-Konto |
| PurchaseDate | DATE | NOT NULL | Anschaffungsdatum |
| PurchasePrice | REAL | NOT NULL | Anschaffungskosten |
| UsefulLifeYears | INTEGER | NOT NULL | Nutzungsdauer |
| DepreciationMethod | TEXT | DEFAULT 'linear' | AfA-Methode |
| SerialNumber | TEXT | | Seriennummer |
| Location | TEXT | | Standort |
| Supplier_ID | INTEGER | FK | Lieferant |
| Document_ID | INTEGER | FK | Kaufbeleg |
| Booking_ID | INTEGER | FK | Anschaffungsbuchung |
| SaleDate | DATE | | Verkaufsdatum |
| SalePrice | REAL | | Verkaufspreis |
| Status | TEXT | DEFAULT 'active' | 'active', 'disposed', 'sold' |
| Notes | TEXT | | Notizen |
| Parent_ID | INTEGER | FK | Übergeordnetes Anlagegut |
| CreatedAt | DATETIME | DEFAULT NOW | Erstellungszeitpunkt |

**Foreign Keys:**
- AssetCategory_ID → AssetCategories(ID)
- COA_ID → ChartOfAccounts(ID)
- Supplier_ID → Contacts(ID)
- Document_ID → Documents(ID)
- Booking_ID → Bookings(ID)
- Parent_ID → Assets(ID)

**Indizes:** UNIQUE(InventoryNumber)

---

### 15. AssetDepreciations (AfA-Pläne)

**Zweck:** Geplante und gebuchte Abschreibungen

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment ID |
| Asset_ID | INTEGER | NOT NULL, FK | Anlagegut |
| Year | INTEGER | NOT NULL | Jahr |
| DepreciationAmount | REAL | NOT NULL | AfA-Betrag |
| BookValue | REAL | NOT NULL | Restbuchwert |
| Booking_ID | INTEGER | FK | Zugehörige Buchung |
| Status | TEXT | DEFAULT 'planned' | 'planned', 'booked' |
| BookedAt | DATETIME | | Buchungszeitpunkt |

**Foreign Keys:**
- Asset_ID → Assets(ID) ON DELETE CASCADE
- Booking_ID → Bookings(ID)

**Indizes:** UNIQUE(Asset_ID, Year)  
**Business Logic:** Ein Datensatz pro Asset und Jahr

---

## Datenfluss & Beziehungen

### Rechnungserstellung (XRechnung)

```
1. NumberRanges → generiert InvoiceNumber
2. Contacts (Type='own') → SellerName, SellerCompany, ...
3. Contacts (Type='customer') → BuyerName, BuyerCompany, ...
4. Accounts → BankName, BankIBAN, BankBIC
5. Articles → werden kopiert in InvoiceItems
6. Invoice gespeichert mit Status='draft'
7. PDF-Generierung: pdf_generator.py → PDFPath
8. Status-Änderung: draft → finalized → sent → paid
```

### Buchung mit Beleg

```
1. Documents → Beleg hochladen/scannen
2. Bookings → Buchungssatz erstellen
3. BookingDocuments → Verknüpfung
4. Optional: BookingGroups bei Splitbuchung
```

### AfA-Berechnung

```
1. AssetCategories → Nutzungsdauer definiert
2. Assets → Anlagegut erfassen
3. AssetDepreciations → automatisch generierte AfA-Pläne
4. Bookings → gebuchte Abschreibungen
```

---

## Wichtige Index-Mappings (für SELECT *)

### Invoices Tabelle (45 Spalten, 0-basiert)

```python
INDEX_MAP = {
    'ID': 0,
    'InvoiceNumber': 1,
    'InvoiceDate': 2,
    'OwnCompanyId': 3,
    # ... Seller Fields 4-12 ...
    'CustomerId': 13,
    'BuyerName': 14,  # WICHTIG: Offset +1 bei LIST queries!
    # ... Buyer Fields 15-22 ...
    'OrderNumber': 23,
    'Currency': 24,
    'DeliveryDate': 25,
    'PaymentTerms': 26,
    'PaymentDueDate': 27,
    'SkontoDays': 28,
    'SkontoPercent': 29,
    'BankAccountId': 30,  # KRITISCH: War fälschlich 29!
    # ... Bank Fields 31-33 ...
    'TaxCategory': 34,
    'TaxRate': 35,
    'SumNet': 36,
    'TaxAmount': 37,
    'SumGross': 38,
    'AmountDue': 39,
    'Status': 40,
    'PDFPath': 41,
    'XMLPath': 42,
    'CreatedAt': 43,
    'UpdatedAt': 44
}
```

**ACHTUNG:** Bei Joins oder subqueries kann sich der Index verschieben!

---

## Dateistruktur & Conventions

```
data/
├── buch.db                          # SQLite Datenbank
├── invoices/                        # Generierte PDFs
│   └── YYYY/                        # Jahr-basierte Ordner
│       └── Rechnung_YYYYLNNN.pdf    # Format: Jahr+Letter+Nummer
└── documents/                       # Hochgeladene Belege (optional)
```

---

## SQL-Export & Import

### SQL-Export (`export_to_sql()`)

Exportiert alle Tabellendaten als INSERT-Statements:
- **Kompaktes Format**: Multi-Value INSERT-Syntax für kürzere Dateien
  ```sql
  INSERT INTO TableName (col1, col2, col3) VALUES
  (val1, val2, val3),
  (val1, val2, val3),
  (val1, val2, val3);
  ```
- **Direkt verwendbar**: Statements können direkt in SQL-Konsole eingefügt werden
- **Ausgabe**: `data/db-export.sql`
- **Überspringt**: Leere Tabellen und SQLite-interne Tabellen

### WISO Mein Büro CSV-Import (`import_wiso_csv()`)

Importiert Buchungen aus WISO Mein Büro mit automatischer Format-Erkennung:

#### Unterstützte Formate:

**1. Original-Export (9 Spalten):**
- **CSV-Format**: ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
- **Mapping**:
  - KONTO → ChartOfAccounts.AccountNumber → COA_ID (Sollkonto)
  - GEGENKONTO → ChartOfAccounts.AccountNumber → CounterCOA_ID (Habenkonto)
  - SCHLUESSEL → BU-Schlüssel → TaxRate (401=19%, 402=7%, 121=0%)
- **Duplikat-Erkennung**: REFERENZNUMMER + Datum + COA_ID + Betrag
- **Aktion**: Neue Buchungen anlegen

**2. Tabellen-Export (6 Spalten):**
- **CSV-Format**: Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr./opt. Beleg Nr.;Betrag
  - Spalte 1 (Status) und 8 (Saldo) sollten vor dem Export entfernt werden
  - Spaltenname "Beleg Nr." oder "opt. Beleg Nr." wird automatisch erkannt
- **Mapping**:
  - Empf./Auft. → RecipientClient
  - Verwendungszweck → Text (Zeilenumbrüche werden in Leerzeichen konvertiert)
  - Kategorie (SKR-Beschreibung) → COA_ID (automatisches Matching)
  - Beleg Nr. → DocumentNumber
- **Suche**: Datum + DocumentNumber + Betrag
- **Aktion**: 
  - Bestehende Buchungen aktualisieren (UPDATE)
  - Neue Buchungen anlegen wenn nicht gefunden (INSERT)
  - Nur leere Felder werden ergänzt (keine Überschreibung)
- **Zeilenumbrüche**: Textfelder mit Zeilenumbrüchen (z.B. Überweisungstexte) werden automatisch normalisiert

#### Format-Erkennung:
- Automatisch anhand der Spaltenüberschriften
- Original: erkennt "KONTO" und "GEGENKONTO"
- Tabelle: erkennt "Empf./Auft." und "Verwendungszweck"

#### Gemeinsame Features:
- **Encoding**: Automatische Erkennung (CP1252, UTF-8-SIG, UTF-8, Latin-1)
- **Fehlerbehandlung**: 
  - Fehlende SKR-Konten werden gemeldet
  - Duplikate werden übersprungen mit Details
  - Fehlerhafte Zeilen werden protokolliert
- **Rückgabe**: 
  ```python
  {
    'imported': int,        # Neu angelegte Buchungen
    'updated': int,         # Aktualisierte Buchungen (nur Tabellen-Format)
    'skipped': int,         # Übersprungene Duplikate
    'errors': list[str],    # Fehlermeldungen
    'format': str          # 'original' oder 'table'
  }
  ```

## SQL-Logging

Die Klasse `Database` unterstützt optionales SQL-Logging:
- Automatische Ersetzung von Parametern in Templates
- Formatierung für lesbare Logs
- Aktiviert durch `log_description` Parameter in insert/update Methoden

---

## Besonderheiten & Constraints

1. **Foreign Keys:** Aktiviert via `PRAGMA foreign_keys = ON`
2. **ON DELETE CASCADE:** Nur bei InvoiceItems und AssetDepreciations
3. **Snapshot-Prinzip:** Invoices kopieren Stammdaten zum Zeitpunkt der Erstellung
4. **Datum-Format:** ISO 8601 (YYYY-MM-DD)
5. **Währung:** Standardmäßig EUR, erweiterbar
6. **Steuersätze:** Als Dezimalzahl (0.19 = 19%)

---

## Migrations & Setup

- **initialize_database():** Erstellt alle Tabellen beim ersten Start
- **_run_migrations():** Fügt neue Tabellen hinzu (Assets, AssetCategories, etc.)
- **ensure_kasse_exists():** Erstellt Default-Konto "Kasse"
- **_seed_asset_categories():** Befüllt AfA-Kategorien nach BMF

---

## Performance-Hinweise

- Keine expliziten Indizes außer PRIMARY KEYs und UNIQUE Constraints
- Bei großen Datenmengen empfohlen:
  - Index auf `Bookings.DateBooking`
  - Index auf `Invoices.Status`
  - Index auf `Documents.Date`

---

**Dokumentversion:** 1.1  
**Erstellt:** 26. Februar 2026  
**Letzte Aktualisierung:** 17. März 2026  
**Wartung:** Bei Schemaänderungen aktualisieren!

---

## Änderungshistorie

**v1.1 (17. März 2026)**
- Neues Feld `CounterCOA_ID` in Bookings-Tabelle für doppelte Buchführung
- Kompaktes SQL-Export-Format (Multi-Value INSERT)
- WISO Mein Büro CSV-Import mit Duplikat-Erkennung nach Datum

**v1.0 (26. Februar 2026)**
- Initiale Dokumentation
