# Datenbankmodell PyBuch

**Status:** Aktuell (Stand: 7. Mai 2026)
**DBMS:** SQLite 3
**Datei:** `data/buch.db`

---

## Übersicht

PyBuch verwaltet Buchführung für kleine/mittlere Unternehmen mit Fokus auf deutsche Buchhaltungsstandards (SKR03/SKR04). Es unterstützt XRechnung-konforme Rechnungserstellung, Anlagenverwaltung (AfA), automatische Bank↔Entry-Verknüpfung und Belegmanagement.

---

## Tabellen-Übersicht

```
┌─────────────────────┐
│ STAMMDATEN          │
├─────────────────────┤
│ • ChartOfAccounts   │ Kontenrahmen (SKR03/04/07)
│ • Accounts          │ Bankkonten/Kasse (mit SKRAccount-Zuordnung)
│ • Contacts          │ Kunden/Lieferanten/Eigene Firma (Basis)
│ • ContactAddresses  │ Adressen zu Kontakten (1:n)
│ • CompanyDetails    │ Unternehmensdetails (1:1)
│ • PersonDetails     │ Personendetails (1:1)
│ • Articles          │ Artikel-/Dienstleistungskatalog
│ • Categories        │ Kategorien für Privatbelege
│ • AssetCategories   │ AfA-Kategorien nach BMF
│ • TaxKeys           │ DATEV-Steuerschlüssel (BU-Codes)
└─────────────────────┘

┌─────────────────────┐
│ GESCHÄFTSVORFÄLLE   │
├─────────────────────┤
│ • Bookings          │ Buchungssätze (bank + entry)
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
│ • InvoicePayments   │ Zahlungsverknüpfungen
└─────────────────────┘

┌─────────────────────┐
│ ANLAGENVERWALTUNG   │
├─────────────────────┤
│ • Assets            │ Anlageverzeichnis
│ • AssetDepreciations│ AfA-Pläne
└─────────────────────┘
```

---

## Seed-Daten

Beim Erstellen der Datenbank werden folgende Tabellen automatisch befüllt (nur wenn leer):

| Tabelle | Quelle | Einträge |
|---------|--------|----------|
| ChartOfAccounts | `seed_data/chart_of_accounts_skr04.json` + optional `seed_data/private/chart_of_accounts_custom.json` | 56 Standard + eigene |
| AssetCategories | `seed_data/asset_categories.json` | 30 BMF-Kategorien |
| TaxKeys | `seed_data/tax_keys.json` | 50 DATEV-BU-Schlüssel |

---

## Detaillierte Tabellendefinitionen

### 1. ChartOfAccounts (Kontenrahmen)

**Zweck:** Standard-Kontenrahmen (SKR03/04 für DE, SKR07 für AT). Wird beim DB-Erstellen aus `seed_data/` geseeded.

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Framework | INTEGER | UNIQUE(Framework, AccountNumber) | 3=SKR03, 4=SKR04, 7=SKR07 |
| AccountNumber | INTEGER | UNIQUE(Framework, AccountNumber) | Kontonummer |
| Name | TEXT | | Kontobezeichnung |
| Description | TEXT | | Zusätzliche Beschreibung |
| IsStandard | INTEGER | DEFAULT 0 | 1=Standard-Konto, 0=eigene Ergänzung |
| PrivateSharePercent | INTEGER | DEFAULT 0 | Privatanteil in % (0–100); wird bei Matching/Export berücksichtigt |

---

### 2. Accounts (Bank-/Kassenkonten)

**Zweck:** Verwaltung der eigenen Bankkonten und Kasse

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Name | TEXT | NOT NULL, UNIQUE | Kontoname |
| Owner | TEXT | | Kontoinhaber |
| Number | TEXT | | IBAN oder Kreditkartennummer |
| BIC | TEXT | | BIC/SWIFT-Code |
| BankName | TEXT | | Name der Bank |
| IsCash | INTEGER | DEFAULT 0 | 1=Kasse, 0=Bankkonto |
| SKRAccount | INTEGER | | Zugeordnete SKR-Kontonummer (z.B. 1810) |

**Business Logic:**
- Konto "Kasse" wird automatisch beim Start erstellt
- `SKRAccount` verknüpft mit ChartOfAccounts.AccountNumber für Doppik-Erkennung
- Wird von `_get_bank_coa_ids()` genutzt um echte Bankkonten zu identifizieren

---

### 3. Contacts – normalisiertes Schema (4 Tabellen)

#### 3a. Contacts (Basis)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| ContactType | TEXT | DEFAULT 'customer' | 'customer', 'supplier', 'own', 'insurance', 'other' |
| EntityType | TEXT | DEFAULT 'company' | 'company' oder 'person' |
| DisplayName | TEXT | | Anzeigename |
| CustomerNumber | TEXT | UNIQUE | Kundennummer |
| Email | TEXT | | E-Mail |
| Phone | TEXT | | Telefon |
| Notes | TEXT | | Notizen |
| Logo | TEXT | | Pfad zum Firmenlogo |

#### 3b. ContactAddresses (1:n)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| ContactID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | |
| AddressType | TEXT | DEFAULT 'main' | 'main', 'billing', 'delivery' |
| AddressLine1 | TEXT | | Adresszusatz / c/o |
| Street | TEXT | | Straße + Hausnummer |
| PostalCode | TEXT | | PLZ |
| City | TEXT | | Ort |
| Country | TEXT | DEFAULT 'DE' | ISO-Ländercode |

**Indizes:** UNIQUE(ContactID, AddressType)

#### 3c. CompanyDetails (1:1)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ContactID | INTEGER | UNIQUE, FK → Contacts ON DELETE CASCADE | |
| CompanyName | TEXT | | Firmenname |
| LegalForm | TEXT | | Rechtsform (GmbH, AG, UG, ...) |
| TaxID | TEXT | | Umsatzsteuer-ID |
| BuyerRouteID | TEXT | | XRechnung: Leitweg-ID |

#### 3d. PersonDetails (1:1)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ContactID | INTEGER | UNIQUE, FK → Contacts ON DELETE CASCADE | |
| Salutation | TEXT | | Anrede |
| Title | TEXT | | Titel (Dr., Prof.) |
| FirstName | TEXT | | Vorname |
| LastName | TEXT | | Nachname |
| DateOfBirth | TEXT | | ISO 8601 |
| CompanyContactID | INTEGER | FK → Contacts | Zugehöriges Unternehmen |
| CompanyName_Free | TEXT | | Freier Firmenname |

---

### 4. Articles (Artikelstamm)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Name | TEXT | NOT NULL | Artikelbezeichnung |
| Unit | TEXT | DEFAULT 'Stk.' | Einheit |
| UnitPrice | REAL | DEFAULT 0 | Netto-Einzelpreis |
| TaxRate | REAL | DEFAULT 19 | Steuersatz in % |
| Description | TEXT | | Beschreibung |
| Active | INTEGER | DEFAULT 1 | 1=aktiv, 0=inaktiv |

---

### 5. Categories

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Name | TEXT | | Kategoriename |
| Text | TEXT | | Zusatzinfos |
| Parent_ID | INTEGER | | Selbstreferenz (Hierarchie) |

---

### 6. TaxKeys (DATEV-Steuerschlüssel)

**Zweck:** BU-Schlüssel nach DATEV-Standard. Wird aus `seed_data/tax_keys.json` geseeded (50 Einträge).

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| Code | TEXT | PRIMARY KEY | BU-Schlüssel (z.B. '9', '401') |
| Description | TEXT | NOT NULL | Beschreibung |
| TaxRate | REAL | | Steuersatz als Dezimal (0.19 = 19%), NULL = kein Satz |
| TaxType | TEXT | | 'USt', 'VSt', 'steuerfrei', '§13b', 'keine', 'UStfrei' |

**Verwendung:**
- WISO-Original-Import: SCHLUESSEL → `get_tax_rate_for_bu()` → TaxRate
- TaxAmount-Berechnung: `|Brutto| - |Brutto| / (1 + Rate)`

---

### 7. Documents (Belege)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Number | TEXT | | Belegnummer |
| Date | DATE | | Belegdatum |
| Filename | TEXT | | Dateiname |
| Path | TEXT | | Dateipfad |
| Info | TEXT | | Zusatzinformationen |

---

### 8. BookingGroups (Splitbuchungen)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Description | TEXT | | Beschreibung |
| CreatedDate | DATE | | Erstellungsdatum |
| TotalAmount | REAL | | Gesamtbetrag (Validierung) |

---

### 9. Bookings (Buchungssätze)

**Zweck:** Zentrale Tabelle für Bank-Bewegungen und Buchungssätze. Über `BookingType` und `ParentBooking_ID` werden Bankbewegungen mit ihren Buchungssätzen verknüpft.

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY | Auto-Increment |
| DateBooking | 1 | DATE | NOT NULL | Buchungsdatum |
| DateTax | 2 | DATE | | Steuerdatum |
| BookingGroup_ID | 3 | INTEGER | FK → BookingGroups | Splitbuchung |
| Account_ID | 4 | INTEGER | FK → Accounts | Eigenes Bank-/Kassenkonto |
| ForeignBankAccount | 5 | TEXT | | Fremde IBAN/Kontonummer |
| RecipientClient | 6 | TEXT | | Empfänger/Auftraggeber |
| Contact_ID | 7 | INTEGER | FK → Contacts | Kontakt-Zuordnung |
| COA_ID | 8 | INTEGER | FK → ChartOfAccounts | SKR-Sollkonto |
| CounterCOA_ID | 9 | INTEGER | FK → ChartOfAccounts | SKR-Gegenkonto (Doppik) |
| Category_ID | 10 | INTEGER | FK → Categories | Kategorie |
| Amount | 11 | REAL | NOT NULL | Betrag (+Haben, −Soll) |
| Currency | 12 | TEXT | DEFAULT 'EUR' | Währung |
| TaxRate | 13 | REAL | | Steuersatz (0.19 = 19%) |
| TaxAmount | 14 | REAL | | Berechneter Steuerbetrag |
| Text | 15 | TEXT | | Verwendungszweck |
| DocumentNumber | 16 | TEXT | | Belegnummer |
| BookingType | 17 | TEXT | DEFAULT 'entry' | **'bank'** = Bankbewegung, **'entry'** = Buchungssatz |
| ParentBooking_ID | 18 | INTEGER | FK → Bookings(ID) | Verknüpfung: Entry → Bank-Buchung |
| Status | 19 | TEXT | | Frei definierbar |

**Business Logic:**
- `BookingType='bank'`: Echte Geldbewegung auf dem Bankkonto (aus WISO-Tabellen-Export oder PDF-Import)
- `BookingType='entry'`: Buchhalterischer Eintrag (aus WISO-Original-Export oder manuell)
- `ParentBooking_ID`: Verknüpft einen Entry mit seiner zugehörigen Bankbuchung
- Doppik-Entries (COA_ID zeigt auf ein SKR-Bankkonto wie 1810) werden in der Anzeige ausgeblendet
- `link_bank_to_entries()` verknüpft automatisch anhand mehrstufigem Matching (siehe Datenfluss)
- `Status='resolved'`: Debitoren-Entry wurde über Stufe 7 als erledigt markiert (Zahlung existiert)

---

### 10. BookingDocuments (n:m)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Booking_ID | INTEGER | NOT NULL, FK → Bookings | |
| Document_ID | INTEGER | NOT NULL, FK → Documents | |
| RelationType | TEXT | | Art der Beziehung |

**Indizes:** UNIQUE(Booking_ID, Document_ID)

---

### 11. NumberRanges

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Type | TEXT | NOT NULL | 'invoice', 'receipt_company', 'receipt_category' |
| Year | INTEGER | NOT NULL | Jahr (4-stellig) |
| Letter | TEXT | NOT NULL | Buchstabe (z.B. 'R') |
| Prefix | TEXT | DEFAULT '' | Zusätzlicher Präfix |
| CurrentNumber | INTEGER | DEFAULT 0 | Letzte vergebene Nummer |
| Description | TEXT | | Beschreibung |

**Indizes:** UNIQUE(Type, Year, Letter, Prefix)

---

### 12. Invoices (Ausgangsrechnungen)

**Zweck:** XRechnung-konforme Rechnungsverwaltung mit Snapshot-Prinzip

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY | |
| InvoiceNumber | 1 | TEXT | UNIQUE, NOT NULL | Rechnungsnummer |
| InvoiceDate | 2 | DATE | NOT NULL | |
| **Verkäufer (Snapshot)** | | | | |
| OwnCompanyId | 3 | INTEGER | FK → Contacts | |
| SellerName | 4 | TEXT | NOT NULL | |
| SellerCompany | 5 | TEXT | NOT NULL | Firmenname |
| SellerStreet | 6 | TEXT | | |
| SellerPostalCode | 7 | TEXT | | |
| SellerCity | 8 | TEXT | | |
| SellerCountry | 9 | TEXT | DEFAULT 'DE' | |
| SellerVATID | 10 | TEXT | | USt-ID |
| SellerEmail | 11 | TEXT | | |
| SellerPhone | 12 | TEXT | | |
| **Käufer (Snapshot)** | | | | |
| CustomerId | 13 | INTEGER | FK → Contacts | |
| BuyerName | 14 | TEXT | NOT NULL | |
| BuyerCompany | 15 | TEXT | NOT NULL | Firmenname |
| BuyerStreet | 16 | TEXT | | |
| BuyerPostalCode | 17 | TEXT | | |
| BuyerCity | 18 | TEXT | | |
| BuyerCountry | 19 | TEXT | DEFAULT 'DE' | |
| BuyerVATID | 20 | TEXT | | |
| BuyerReference | 21 | TEXT | | Kundenreferenz |
| BuyerRouteID | 22 | TEXT | | Leitweg-ID (XRechnung) |
| **Auftrag / XRechnung** | | | | |
| OrderNumber | 23 | TEXT | | |
| Currency | 24 | TEXT | DEFAULT 'EUR' | |
| DeliveryDate | 25 | DATE | | |
| **Zahlungsbedingungen** | | | | |
| PaymentTerms | 26 | TEXT | | |
| PaymentDueDate | 27 | DATE | | |
| SkontoDays | 28 | INTEGER | | |
| SkontoPercent | 29 | REAL | | |
| **Bankverbindung (Snapshot)** | | | | |
| BankAccountId | 30 | INTEGER | FK → Accounts | |
| BankName | 31 | TEXT | | |
| BankIBAN | 32 | TEXT | | |
| BankBIC | 33 | TEXT | | |
| **Summen** | | | | |
| TaxCategory | 34 | TEXT | DEFAULT 'S' | |
| TaxRate | 35 | REAL | NOT NULL | 0.19 = 19% |
| SumNet | 36 | REAL | NOT NULL | |
| TaxAmount | 37 | REAL | NOT NULL | |
| SumGross | 38 | REAL | NOT NULL | |
| AmountDue | 39 | REAL | NOT NULL | |
| **Verwaltung** | | | | |
| Status | 40 | TEXT | DEFAULT 'draft' | draft/finalized/sent/paid/cancelled |
| PDFPath | 41 | TEXT | | |
| XMLPath | 42 | TEXT | | |
| CreatedAt | 43 | DATETIME | DEFAULT NOW | |
| UpdatedAt | 44 | DATETIME | | |

---

### 13. InvoiceItems

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| InvoiceId | INTEGER | NOT NULL, FK → Invoices ON DELETE CASCADE | |
| Position | INTEGER | NOT NULL | |
| ArticleId | INTEGER | FK → Articles | |
| Description | TEXT | NOT NULL | |
| Quantity | REAL | NOT NULL | |
| Unit | TEXT | DEFAULT 'C62' | UN/ECE Code |
| PricePerUnit | REAL | NOT NULL | Netto |
| TotalNet | REAL | NOT NULL | Quantity × PricePerUnit |
| TaxCategory | TEXT | DEFAULT 'S' | |
| TaxRate | REAL | NOT NULL | |

---

### 14. InvoicePayments

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| InvoiceID | INTEGER | FK → Invoices | |
| BookingID | INTEGER | FK → Bookings | |
| Amount | REAL | | Zahlungsbetrag |
| PaymentDate | DATE | | |
| Notes | TEXT | | |

---

### 15. AssetCategories (AfA-Kategorien)

**Zweck:** BMF-Tabellen für Anlagegüter. Geseeded aus `seed_data/asset_categories.json` (30 Einträge).

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| Name | TEXT | NOT NULL | Kategoriename |
| UsefulLifeYears | INTEGER | NOT NULL | Nutzungsdauer |
| DepreciationMethod | TEXT | DEFAULT 'linear' | 'linear', 'declining', 'both' |
| COA_ID | INTEGER | FK → ChartOfAccounts | Standard-SKR-Konto |
| Notes | TEXT | | |

---

### 16. Assets (Anlagenverzeichnis)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| InventoryNumber | TEXT | UNIQUE | Auto: `INV-YY-###` |
| Name | TEXT | NOT NULL | |
| Description | TEXT | | |
| AssetCategory_ID | INTEGER | FK → AssetCategories | |
| COA_ID | INTEGER | FK → ChartOfAccounts | |
| PurchaseDate | DATE | NOT NULL | |
| PurchasePrice | REAL | NOT NULL | Netto |
| UsefulLifeYears | INTEGER | NOT NULL | |
| DepreciationMethod | TEXT | DEFAULT 'linear' | |
| SerialNumber | TEXT | | |
| Location | TEXT | | |
| Supplier_ID | INTEGER | FK → Contacts | |
| Document_ID | INTEGER | FK → Documents | |
| Booking_ID | INTEGER | FK → Bookings | |
| SaleDate | DATE | | |
| SalePrice | REAL | | |
| Status | TEXT | DEFAULT 'active' | 'active', 'disposed', 'sold' |
| Notes | TEXT | | |
| Parent_ID | INTEGER | FK → Assets | Erweiterungen |
| CreatedAt | DATETIME | DEFAULT NOW | |

---

### 17. AssetDepreciations (AfA-Pläne)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| Asset_ID | INTEGER | NOT NULL, FK → Assets ON DELETE CASCADE | |
| Year | INTEGER | NOT NULL | |
| DepreciationAmount | REAL | NOT NULL | AfA-Betrag |
| BookValue | REAL | NOT NULL | Restbuchwert |
| Booking_ID | INTEGER | FK → Bookings | |
| Status | TEXT | DEFAULT 'planned' | 'planned', 'booked' |
| BookedAt | DATETIME | | |

**Indizes:** UNIQUE(Asset_ID, Year)

---

## Datenfluss

### Bank↔Entry-Verknüpfung

```
1. WISO-Tabellen-Export  → Bookings mit BookingType='bank' (Bankbewegungen)
2. WISO-Original-Export  → Bookings mit BookingType='entry' (Buchungssätze)
   (inkl. TaxRate aus TaxKeys-DB, TaxAmount berechnet)
3. link_bank_to_entries() verknüpft automatisch:
   - Stufe 1: Datum + normalisierter Empfänger + Betrag
   - Stufe 2: Datum + Betrag (eindeutig nach Doppik-Filter)
   - Stufe 3: Split-Gruppen mit Summenabgleich (nur gleicher Tag)
   - Stufe 3b: Rechnungs-Split (SUM/Anzahl, Bank-COA als Marker)
   - Stufe 3c: Privatanteil-Split (Summe minus Privatentnahme-Offset)
   - Stufe 3d: Sammelzahlung (mehrere Rechnungsnummern im Bank-Text)
   - Stufe 4: DocumentNumber als Tiebreaker bei Mehrdeutigkeit
   - Stufe 5: Text-Token-Matching (lange Ziffernfolgen >= 8 Stellen)
   - Stufe 6: Text-Similarity ohne Belegnummer (SequenceMatcher)
   - Stufe 7: Debitoren-Auflösung (Status='resolved' für Debitoren-Entries
     deren Zahlung bereits verknüpft ist, datumsunabhängig)
4. Entry.ParentBooking_ID → Bank.ID
5. Doppik-Entries (COA_ID = Bankkonto-SKR) werden ausgeblendet
```

### EÜR-Ableitung (Dashboard)

Die EÜR-Werte werden aus `Bookings` abgeleitet (nicht aus Rechnungen):

- Betriebliche Kontensalden werden netto aggregiert (`Amount - TaxAmount`)
- Virtuelles USt-Konto 3806 enthält nur Steueranteile aus Einnahmekonten
- Virtuelle Vorsteuerkonten 1401/1406 enthalten Steueranteile aus Ausgabenkonten
- Konten 3160, 3720, 3740 werden im Dashboard separat als "Sonstige Ausgaben" gezeigt

### Rechnungserstellung

```
1. NumberRanges → InvoiceNumber
2. Contacts (Type='own') → Seller-Snapshot
3. Contacts (Type='customer') → Buyer-Snapshot
4. Accounts → Bank-Snapshot
5. Articles → InvoiceItems
6. Status: draft → finalized → sent → paid
```

### AfA-Berechnung

```
1. AssetCategories → Nutzungsdauer + Methode
2. Assets → Erfassung
3. AssetDepreciations → automatische AfA-Pläne
4. Buchungsintegration über Booking_ID
```

---

## Besonderheiten

1. **Foreign Keys:** `PRAGMA foreign_keys = ON`
2. **ON DELETE CASCADE:** InvoiceItems, AssetDepreciations, ContactAddresses, CompanyDetails, PersonDetails
3. **Snapshot-Prinzip:** Invoices kopieren Stammdaten bei Erstellung
4. **Datum-Format:** ISO 8601 (YYYY-MM-DD)
5. **Steuersätze:** Dezimal (0.19 = 19%)
6. **Seeding:** `_seed_chart_of_accounts()`, `_seed_asset_categories()`, `_seed_tax_keys()` laden aus `seed_data/`-JSON

---

**Dokumentversion:** 2.1
**Letzte Aktualisierung:** 7. Mai 2026
