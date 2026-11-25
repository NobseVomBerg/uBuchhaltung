# Datenbankmodell uBuchhaltung

**Stand:** Schema 2010 (`version.SCHEMA_VERSION`)
**DBMS:** SQLite 3
**Datei:** `data/users/<Name>/buch.db`

> WISO, WISO Mein Büro und DATEV sind Marken ihrer jeweiligen Inhaber. uBuchhaltung ist ein
> unabhängiges Projekt und steht in keiner Verbindung zu den Markeninhabern oder den Herstellern.
> Die Namen dienen allein der Bezeichnung der unterstützten Import- und Exportformate.

Diese Datei ist das Nachschlagewerk zu Tabellen, Spalten und Datenfluss. **Regeln, Schutzregeln und Grenzen stehen in
`docs/regeln/`** (Tabelle in `CLAUDE.md`); hier steht je Abschnitt nur, was die Tabelle oder der Fluss tut, und der Verweis.
Schema und Spaltenreihenfolge definiert `db/schema.py`, die Migrationsschritte je Stufe `docs/regeln/versionierung.md`.

---

## Übersicht

uBuchhaltung verwaltet die Buchführung kleiner Unternehmen (SKR03/SKR04, ausgeliefert ist SKR04), erzeugt Rechnungen und Angebote
(PDF, ZUGFeRD/CII, XRechnung/UBL), führt ein Anlagenverzeichnis (AfA) und verknüpft Bankbewegungen mit Buchungssätzen und Belegen.

| Bereich | Tabellen |
|---|---|
| Stammdaten | `ChartOfAccounts`, `Accounts`, `Contacts`, `ContactAddresses`, `CompanyDetails`, `PersonDetails`, `ContactTypeLinks`, `PersonRoles`, `Articles`, `Categories`, `AssetCategories`, `TaxKeys`, `TaxRates` |
| Geschäftsvorfälle | `Bookings`, `BankTransactionDetails`, `BookingExports`, `Documents`, `DocumentReadings`, `BookingDocuments` |
| Rechnungswesen | `NumberRanges`, `Invoices` (Rechnungen **und** Angebote, `DocumentType`), `InvoiceItems`, `InvoicePayments`, `InvoiceCorrections`, `InvoiceDocumentVersions` |
| Zeiterfassung | `WorkTimes`, `Trips` |
| Anlagen | `Assets`, `AssetDepreciations` |

---

## Seed-Daten

Beim Erstellen der Datenbank werden diese Tabellen befüllt, **nur wenn sie leer sind** (`db/seed.py`):

| Tabelle | Quelle | Einträge |
|---------|--------|----------|
| ChartOfAccounts | `seed_data/chart_of_accounts_skr04.json` (+ optional `seed_data/private/`, bleibt im Repo leer) | 96 Standardkonten (SKR04) |
| AssetCategories | `seed_data/asset_categories.json` | 30 BMF-Kategorien |
| TaxKeys | `seed_data/tax_keys.json` | 53 DATEV-BU-Schlüssel |

`TaxRates` füllt sich nicht wieder: Die Vorbelegung (je Satz aus `tax_groups.MIXED_TAX_RATES`, 19 % und 7 %) entsteht genau einmal beim
Anlegen der Tabelle (`_ensure_tax_rates_schema`); wer alle Einträge löscht, bekommt keinen zurück. Fehlende Standardkonten eines Bestands
legt nur die Aktion „Standardkonten ergänzen“ an (`missing_standard_accounts`/`add_missing_standard_accounts`), nie der Start
(Regeln: `docs/regeln/buchungen.md`). Die Kasse legt `ensure_kasse_exists` an.

**Testdaten** (`seed_data/test/`, nur über `load_test_seed_data()`, idempotent): vom Demo-Modus (ohne Rechnungen und mit
`include_dated=False` ohne die fest datierten Buchungen, Anlagen, Angebote, Arbeitszeiten – die erzeugt `demo_history`) und von den Tests;
die Oberfläche lädt sie nicht. Dateien: `test_accounts`, `test_articles` (50), `test_assets` (22), `test_bookings`, `test_contacts` (33),
`test_documents`, `test_invoices` (50), `test_quotes`, `test_worktimes`, dazu `demo_history`/`demo_scenario` (Regeln: `docs/regeln/werkzeuge.md`).

---

## Tabellendefinitionen

Beträge sind `INTEGER` in Minor Units (1 = 0,0001 EUR, `money.SCALE` = 4), Daten ISO 8601, Steuersätze in `Bookings`/`DocumentReadings`
als Anteil (0.19). Die meisten Tabellen tragen keine CHECK-Regeln; was ein Feld tragen darf, prüft die Anwendung.

### 1. ChartOfAccounts (Kontenrahmen)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| Framework | INTEGER | UNIQUE(Framework, AccountNumber) | 3 = SKR03, 4 = SKR04, 7 = SKR07 |
| AccountNumber | INTEGER | s. o. | Kontonummer |
| Name | TEXT | | Kontobezeichnung |
| Description | TEXT | | Zusatz |
| IsStandard | INTEGER | DEFAULT 0 | 1 = Standardkonto, 0 = eigene Ergänzung |
| PrivateSharePercent | INTEGER | DEFAULT 0 | Privatanteil in % (0–100); wird gepflegt und angezeigt, aber in keiner Auswertung (EÜR, Dashboard, DATEV-Export, Abgleich) berücksichtigt |
| ShowInMenu | INTEGER | DEFAULT 1 | 0 = im Auswahlmenü ausgeblendet |

Ein Konto, das in Buchungen, Anlagen, Steuersätzen oder Rechnungspositionen steht, lässt sich nicht löschen (`Database.coa_is_referenced`).

### 2. Accounts (Bank-/Kassenkonten)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| Name | TEXT | NOT NULL, UNIQUE | Kontoname |
| Owner | TEXT | | Kontoinhaber |
| Number | TEXT | | IBAN oder Kreditkartennummer |
| BIC | TEXT | | |
| BankName | TEXT | | |
| IsCash | INTEGER | DEFAULT 0 | 1 = Kasse |
| SKRAccount | INTEGER | | zugeordnete SKR-Kontonummer (= `ChartOfAccounts.AccountNumber`, z. B. 1810); die Kasse trägt neu 1600 |
| BusinessOnly | INTEGER | NOT NULL, DEFAULT 1 | 1 = reines Geschäftskonto (Kontoauszug wird vollständig importiert), 0 = gemischt genutzt (der Import lässt Umsätze auswählen). Streng gelesen (`db.accounts.is_business_only`): nur die ganze Zahl 0 heißt „gemischt“, die Kasse nie. Ändert keine Buchung und keine Auswertung |

Die Menge der **Zahlungskonten** (`get_payment_coa_ids`: die SKR-Konten der eigenen Bank- und Kassenkonten plus Verrechnungskonto 1460) und
die der Bankkonten (`_get_bank_coa_ids`) bestimmen Vorzeichen der Importe, Form neuer Sätze, Verknüpfung und Split-Summe
(Regeln: `docs/regeln/buchungen.md`, Abschnitt Zahlungskonten).

### 3. Kontakte (6 Tabellen)

**3a. Contacts**

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| ContactType | TEXT | NOT NULL, DEFAULT 'customer' | Haupttyp: `own` (Systemtyp, eigene Firma/Person) oder ein Typschlüssel der Kontaktliste (`customer`, `supplier`, `employee`, `bank` …, `server/pages_contacts.CONTACT_TYPES`) |
| EntityType | TEXT | NOT NULL, DEFAULT 'company' | `company` oder `person` |
| DisplayName | TEXT | | Anzeigename |
| CustomerNumber | TEXT | UNIQUE | Kundennummer |
| Abbreviation | TEXT | UNIQUE | Kürzel (wird aus dem Namen vorgeschlagen) |
| Email, Phone, Notes | TEXT | | |
| Logo | TEXT | | Pfad zum Firmenlogo (Regeln: `docs/regeln/sicherheit.md`) |
| VatMethod | TEXT | | `'ist'` oder `'soll'`; NULL und jeder andere Wert = nicht festgelegt (`db.contacts.vat_method`). Nur der eigene Kontakt trägt sie (`get_vat_method`). **Wird nur gespeichert und angezeigt** – keine Buchung, Auswertung oder Export liest sie |

**3b. ContactAddresses** (1:n): `ID`, `ContactID` (NOT NULL, FK → Contacts, CASCADE), `AddressType` (`main`/`billing`/`delivery`, DEFAULT 'main'),
`AddressLine1` (Zusatz/c/o), `Street`, `PostalCode`, `City`, `Country` (DEFAULT 'DE'). UNIQUE(ContactID, AddressType).

**3c. CompanyDetails** (1:1, `ContactID` PRIMARY KEY, FK CASCADE): `CompanyName`, `LegalForm`, `TaxID` (USt-IdNr. oder Steuernummer, eine Spalte für beides),
`BuyerRouteID` (Leitweg-ID), `DatevConsultantNo` (Beraternummer, 1001–9999999 ohne führende Null), `DatevClientNo` (Mandantennummer, 1–99999); NULL = nicht eingetragen.
Nur die eigene Firma trägt die DATEV-Nummern (`get_datev_numbers`).

**3d. PersonDetails** (1:1, `ContactID` PRIMARY KEY, FK CASCADE): `Salutation`, `Title`, `FirstName`, `LastName`, `DateOfBirth`,
`CompanyContactID` (FK → Contacts: zugehörige Firma), `CompanyName_Free`, `JobTitle`, `Department`, `IsPrimaryContact` (DEFAULT 0).

**3e. ContactTypeLinks** (n:m): `ContactID` (FK CASCADE), `TypeKey` – mehrere Typen je Kontakt; PRIMARY KEY (ContactID, TypeKey). `own` steht nur in `Contacts.ContactType`.

**3f. PersonRoles** (n:m): `ContactID` (FK CASCADE), `RoleKey` (z. B. `invoice_recipient`, `accounting`); PRIMARY KEY (ContactID, RoleKey).

### 4. Articles (Artikelstamm)

`ID`, `Name` (NOT NULL), `Unit` (DEFAULT 'Stk.'), `UnitPrice` (Netto in Minor Units, DEFAULT 0), `TaxRate` (REAL, Prozent, DEFAULT 19),
`Description`, `Active` (DEFAULT 1).

### 5. Categories

`ID`, `Name`, `Text` (Zusatzinfos), `Parent_ID` (Selbstreferenz, Hierarchie).

### 6. TaxKeys (DATEV-Steuerschlüssel)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| Code | TEXT | PRIMARY KEY | BU-Schlüssel (z. B. '9', '401') |
| Description | TEXT | NOT NULL | |
| TaxRate | REAL | | Satz als Anteil (0.19), NULL = kein Satz |
| TaxType | TEXT | | `USt`, `VSt`, `steuerfrei`, `§13b`, `keine`, `UStfrei` |

Dient dem WISO-Import: zuerst die Regeln aus `db/datev_keys.py` (`key_rate`), sonst diese Tabelle; Steuerbetrag = `|Brutto| − |Brutto| / (1 + Satz)`.
Der Export schlüsselt nicht aus dieser Tabelle. Schema 2003 hat 100, 120, 121, 122 und 490 berichtigt (`schema.TAX_KEY_CORRECTIONS`).

### 6a. TaxRates (Steuersätze der Stammdaten)

Die Sätze, aus denen eine Rechnung oder ein Angebot die Sätze ihrer Positionen wählt. Ein Beleg verweist nicht auf einen Eintrag: Seine Positionen
tragen den gewählten Eintrag als Momentaufnahme (Tabelle 13). Regeln: `docs/regeln/rechnungen.md`, Abschnitt „Steuersätze aus den Stammdaten“.

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Label | TEXT | NOT NULL | Bezeichnung (`19 %`) |
| RateBp | INTEGER | NOT NULL | Satz in Hundertstel Prozent: 1900 = 19 %, 550 = 5,5 %, 0 |
| Kind | TEXT | NOT NULL, DEFAULT 'standard' | `standard` (auch Satz 0) \| `exempt` (steuerfrei) |
| ExemptReason | TEXT | | Grund der Steuerbefreiung, nur bei `exempt` |
| RevenueCoaId | INTEGER | FK → ChartOfAccounts | Erlöskonto; NULL = Standardkonto des Satzes (4400 bei 19 %, 4300 bei 7 %) |
| VatCoaId | INTEGER | FK → ChartOfAccounts | Umsatzsteuerkonto (freiwillig, NULL = keines) |
| InputTaxCoaId | INTEGER | FK → ChartOfAccounts | Vorsteuerkonto (freiwillig, NULL = keines) |
| Active | INTEGER | NOT NULL, DEFAULT 1 | 0 = inaktiv |
| SortOrder | INTEGER | NOT NULL, DEFAULT 0 | Reihenfolge in der Auswahl, dann ID |
| CreatedAt | DATETIME | DEFAULT CURRENT_TIMESTAMP | UTC |

### 7. Documents (Belege)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Number | TEXT | | Belegnummer (nicht eindeutig; Nummernkreis `receipt_company`) |
| Date | DATE | | Belegdatum |
| Filename | TEXT | | Dateiname |
| Path | TEXT | | relativ zum Datenordner des Benutzers (`Belege/<Jahr>/<Nummer>_<Name>`, vom Server gesetzt); gilt nur innerhalb von `Belege/` und `Documents/` (Bestand des früheren Uploads), geprüft über `stored_receipt_path` |
| Info | TEXT | | Zusatzinformationen |

Löschen: ein Beleg nur, solange nichts auf ihn verweist (`db.DeleteRefused`; Regeln: `docs/regeln/buchungen.md`, `docs/regeln/belege.md`).

### 7a. DocumentReadings (Lesung eines Belegs)

Die aus der Datei gelesenen Felder (`receipt_reading/`, `server/receipt_reader.py`), getrennt von dem, was der Nutzer am Beleg pflegt. Eine Lesung ist eine
Lesung der Datei, nicht der Inhalt des Belegs: abgeleitete Daten, jederzeit neu lesbar. Kein Wert geht von hier nach `Documents` oder `Bookings`.
Regeln: `docs/regeln/belege.md`.

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Document_ID | INTEGER | NOT NULL, UNIQUE, FK → Documents ON DELETE CASCADE | je Beleg höchstens eine Lesung (eine neue ersetzt die vorige) |
| Status | TEXT | NOT NULL | `ok` \| `empty` \| `no_text` (Bild/Scan, kein OCR) \| `unsupported` \| `too_large` \| `failed` |
| Source | TEXT | | `einvoice_cii` \| `einvoice_ubl` \| `text`; NULL, wo nichts gelesen wurde |
| ReaderVersion | INTEGER | NOT NULL | Stand der Leseregeln (`receipt_reading.READER_VERSION`); ältere Lesung gilt als ungelesen |
| ReadAt | DATETIME | NOT NULL | UTC |
| DocDate | DATE | | gelesenes Belegdatum – nicht `Documents.Date` |
| GrossMinor, NetMinor, TaxMinor | INTEGER | | gelesene Beträge, wie das Dokument sie nennt (eine Gutschrift trägt positive Beträge) |
| TaxRate | REAL | | Anteil (0.19), nur wenn der Beleg genau einen Satz nennt |
| Currency | TEXT | | drei Großbuchstaben; die Textregeln lesen nur `EUR` |
| SenderName, SenderVatId, InvoiceNumber | TEXT | | Absender (≤ 200 Zeichen), USt-IdNr. (≤ 20), Rechnungsnummer (≤ 100) |
| Kind | TEXT | | `invoice` \| `credit_note` |
| Raw | TEXT | | JSON (≤ 64 KB): `fields` mit Herkunft je Feld, `checks`, `tax_breakdown`, `notes` (Schlüssel siehe Kopf von `receipt_reading/__init__.py`) |
| Text | TEXT | | gelesener PDF-Text (≤ 20 000 Zeichen), Eingabe der Vorschläge, auf keiner Seite angezeigt |

Index `idx_documentreadings_gross` auf `GrossMinor`. Geschrieben wird allein über `Database.save_document_reading` (`db/document_readings.py`, eine
`BEGIN IMMEDIATE`-Transaktion; nur wenn Beleg und Pfad unverändert sind), außerhalb des SQL-Protokolls. Die Lesung geht mit dem Beleg (CASCADE) und
zählt nicht als Verweis.

### 8. WorkTimes (Arbeitszeiten)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| PersonID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | Person |
| Date | DATE | NOT NULL | |
| Kind | TEXT | NOT NULL, DEFAULT 'work' | `work`, `vacation`, `sick`, `holiday` |
| CustomerID | INTEGER | FK → Contacts ON DELETE SET NULL | Kunde (optional) |
| StartTime, EndTime | TEXT | | `HH:MM`; Ende vor Beginn = Folgetag (zählt zum Datum des Eintrags) |
| PauseMinutes | INTEGER | DEFAULT 0 | |
| LocationMode | TEXT | DEFAULT 'customer' | `own`, `customer`, `other` |
| LocationCity | TEXT | | aufgelöster Einsatzort (Momentaufnahme) |
| Note | TEXT | | |
| PauseText | TEXT | | Pausenzeiten als Text, z. B. `12:00-12:30` |

Index `idx_worktimes_person_date` (PersonID, Date). Regeln: `docs/regeln/zeiten-fahrten.md`.

### 8a. Trips (Fahrtenbuch)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| DriverID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | Fahrer |
| StartDate | DATE | NOT NULL | |
| StartTime | TEXT | | optional `HH:MM` |
| EndDate, EndTime | DATE, TEXT | | optional |
| StartPoint | TEXT | | leer = eigene Adresse/Firmensitz |
| Destination | TEXT | NOT NULL | Ziel |
| Vehicle | TEXT | | Freitext |
| Reason | TEXT | | |
| DistanceKm | INTEGER | | Kilometer (manuell oder berechnet) |
| StartKm, EndKm | INTEGER | | Tachostand, optional |
| DocumentID | INTEGER | FK → Documents ON DELETE SET NULL | Beleg (optional) |
| CreatedAt | DATETIME | DEFAULT CURRENT_TIMESTAMP | |

Index `idx_trips_driver_date` (DriverID, StartDate). Prüfung beim Speichern (`handlers._trip_fields`) und die Lückenprüfung (`db.trips.logbook_gaps`,
`Database.trip_logbook_check`): `docs/regeln/zeiten-fahrten.md`.

### 9. Bookings (Buchungssätze)

Zentrale Tabelle für Bankbewegungen (`BookingType='bank'`) und Buchungssätze (`'entry'`). Ein Satz unter einer Bankbewegung ist ihr Kind
(`ParentBooking_ID`). Die Spalten werden **positionsbasiert** gelesen (`SELECT *`): neue Spalten immer ans Ende.

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| DateBooking | 1 | DATE | NOT NULL | Buchungsdatum |
| DateTax | 2 | DATE | | Steuerdatum |
| BookingGroup_ID | 3 | INTEGER | | stillgelegt (Schema v5), Splits laufen über `ParentBooking_ID` |
| Account_ID | 4 | INTEGER | FK → Accounts | eigenes Bank-/Kassenkonto |
| ForeignBankAccount | 5 | TEXT | | fremde IBAN/Kontonummer |
| RecipientClient | 6 | TEXT | | Empfänger/Auftraggeber |
| Contact_ID | 7 | INTEGER | FK → Contacts | |
| COA_ID | 8 | INTEGER | FK → ChartOfAccounts | Konto (Seite je nach Form) |
| CounterCOA_ID | 9 | INTEGER | FK → ChartOfAccounts | Gegenkonto |
| Category_ID | 10 | INTEGER | FK → Categories | |
| Amount | 11 | INTEGER | NOT NULL | Minor Units; bei Zahlungen die Richtung der Geldbewegung (Einnahme positiv), bei Umbuchungen positiv = `COA_ID` im Soll |
| Currency | 12 | TEXT | DEFAULT 'EUR' | |
| TaxRate | 13 | REAL | | Anteil (0.19) |
| TaxAmount | 14 | INTEGER | | Steuerbetrag, trägt das Vorzeichen des Betrags |
| Text | 15 | TEXT | | Verwendungszweck |
| DocumentNumber | 16 | TEXT | | Belegnummer |
| BookingType | 17 | TEXT | DEFAULT 'entry' | `bank` = Bankbewegung, `entry` = Buchungssatz |
| ParentBooking_ID | 18 | INTEGER | FK → Bookings | Satz → Bankbewegung |
| Status | 19 | TEXT | | frei definierbar; belegt sind `resolved` (Forderung durch Zahlung erledigt), `posted` (AfA-Buchung), `invoice_payment` (Satz einer Rechnungszahlung) |
| AutoMirror | 20 | INTEGER | DEFAULT 0 | 1 = die App hat den Satz als Spiegel der Bankbewegung angelegt (wird beim Bearbeiten der Bankzeile mitgezogen) |
| SourceGroup | 21 | TEXT | | Klammer aus dem Quellsystem (WISO `ACCOUNTINGID`) |
| EntryForm | 22 | TEXT | | Form des Satzes (unten); Bankzeilen NULL |
| DatevKey | 23 | INTEGER | | BU-Schlüssel, den die Quelle mitgebracht hat (1–9999, NULL = keiner, 0 wird nie gespeichert); der Export gibt ihn unverändert aus, sonst leitet er ab. Nur Importe schreiben ihn; er fällt (NULL), wenn sich Konto, Gegenkonto oder Steuersatz ändern |

Indizes: `idx_bookings_type` (BookingType), `idx_bookings_parent` (ParentBooking_ID), `idx_bookings_group` (BookingGroup_ID), `idx_bookings_date` (DateBooking),
`idx_bookings_sourcegroup` (SourceGroup; der Re-Import des WISO-Datenbankimports sucht darüber). Zusatzangaben zu einer Zeile stehen in eigenen Tabellen (9c, 9d): `Bookings` wird positionsbasiert gelesen und bleibt bei 24 Spalten.

**Form eines Buchungssatzes** (`EntryForm`, `db/matching.py`): steht beim Schreiben fest, statt bei jedem Lesen aus der Kontenliste von heute geraten zu werden.

| Form | Geldseite | Betrag = Richtung auf … | bei Betrag > 0 im Soll |
|------|-----------|-------------------------|------------------------|
| `purpose_first` | Gegenkonto oder leer | dem Gegenkonto | Gegenkonto |
| `liquid_first` | Konto oder leer | dem Konto | Konto |
| `transfer` | keine (beide Seiten kontiert) | – | Konto |
| `mirror_coa` | beide (Spiegel) | dem Konto | Konto |
| `mirror_counter` | beide | dem Gegenkonto | Gegenkonto |
| `mirror_legacy` | beide | unbekannt (Altbestand) | Gegenkonto |

Nur eine Zeile ohne Form (NULL, per SQL-Konsole angelegt) wird mit den Zahlungskonten von jetzt eingeordnet (`classify_entry`). Ableiten, Neuableiten bei
geänderten Konten, Masken (`purpose_in_counter`), Bankwirksamkeit (`is_bank_effective`) und Split-Summe (`bank_effect`): `docs/regeln/buchungen.md`.

**Satzarten ohne Bankbewegung:** Die Forderung einer Rechnung und die AfA-Buchung (`book_depreciation`: „Aufwand an Anlagekonto“, Form `transfer`,
`Status='posted'`, verknüpft über `AssetDepreciations.Booking_ID`) sind eigenständige Sätze ohne `Account_ID` und `ParentBooking_ID`
(Regeln: `docs/regeln/zahlungen.md`, `docs/regeln/buchungen.md`).

### 9a. Offene Posten (Rechnung → Zahlung)

Eine versendete Rechnung bucht die Forderung `10000 Debitoren` an ein Wartekonto; Erlös und Umsatzsteuer entstehen erst mit der Zahlung (Ist-Versteuerung).

| Zeitpunkt | Buchung | Soll → Haben |
|---|---|---|
| Rechnung versendet | Forderung (eigenständiger Satz) | `10000` → Wartekonto |
| Zahlung (Kind der Bankbewegung, `Status='invoice_payment'`) | `Zahlung zu Re. X` | Bankkonto → `10000` |
| Zahlung (dito) | `Umb. zu Re. X` | Wartekonto → Erlöskonto |

Wartekonten je Satz: 19 % → 4405, 7 % → 4345, 0 %/§ 19 → 4340 (`Invoices.OPEN_REVENUE_ACCOUNTS`, `resolve_open_revenue_coa`); Erlöskonten
`resolve_revenue_coa`. Forderungen und Wartekonten sind Bestandskonten und bleiben aus der EÜR heraus (`db.reporting.NEUTRAL_ACCOUNT_NUMBERS`).

Eine **Zahlungszuordnung** ist genau eine `InvoicePayments`-Zeile plus die Sätze, die die App dafür anlegt (Kennung: `ParentBooking_ID` der Bankbewegung,
`DocumentNumber` = Rechnungsnummer, `Status='invoice_payment'`). Die Funktionen: `assign_invoice_payment` und `unassign_invoice_payment`
(`db/invoice_payments.py`), `assign_storno_refund` (Erstattung), `rebuild_invoice_payments` (`db/invoice_repair.py`), `find_payment_inconsistencies`
(Prüfliste, Seite *Daten*, nur auf Klick). Forderung, Erlöskonto, Teil- und Sammelzahlung, Skonto, Entfernen, Prüfliste, Neuaufbau: **`docs/regeln/zahlungen.md`**.

### 9b. Stornorechnung

`Database.cancel_invoice` (`db/invoice_storno.py`, Route `POST /invoice/cancel`) legt einen eigenen Beleg in `Invoices` an (`InvoiceKind='storno'`,
`CancelsInvoiceId` = die stornierte Rechnung, Summen und Positionen exakt negiert), bucht die Gegenbuchung der Forderung und setzt die Rechnung auf
`cancelled`; eine Ersatzrechnung trägt `ReplacesInvoiceId`. Erstattung, Guthaben, Neu ausstellen, Umzug der Zahlungen, Sperre: `docs/regeln/rechnungen.md`
(Abschnitte Stornobeleg, Erstattung, Neu ausstellen, Umzug).

### 9c. BankTransactionDetails (Zusatzfelder einer Bankbewegung)

Höchstens eine Zeile je Bankbewegung (`BookingType='bank'`) mit den Angaben, die ein CAMT-Auszug zusätzlich liefert. Keine Zeile heißt: keine Zusatzfelder (der ganze Bestand). Die Zeile hängt mit
`ON DELETE CASCADE` an der Buchung: Wird die Bewegung gelöscht, geht sie mit.

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| Booking_ID | INTEGER | PRIMARY KEY, FK → Bookings ON DELETE CASCADE | die Bankbewegung |
| ValueDate | DATE | | Valuta (`Ntry/ValDt`), ISO |
| EndToEndId | TEXT | | End-to-End-Referenz (`TxDtls/Refs/EndToEndId`) |
| MandateRef | TEXT | | Mandatsreferenz (`TxDtls/Refs/MndtId`) |
| CreditorId | TEXT | | Gläubiger-ID (SEPA) |
| BankRef | TEXT | | Bankreferenz (`Ntry/AcctSvcrRef`) |

Geschrieben in der Transaktion der Bankzeile (`insert_booking(bank_details=…)`): nur bei einer Bankbewegung und nur, wenn mindestens ein Wert lesbar ist; Leeres steht als NULL. Gelesen wird jeder Wert
erneut über `importers.base.clean_bank_text` bzw. `clean_value_date` (höchstens 35 Zeichen, ohne Steuerzeichen, ohne die Platzhalter `NOTPROVIDED`, `NONREF`, `NOTAVAILABLE`; ein ISO-Datum):
`Database.get_bank_details` liefert die lesbaren Werte, `Database.bank_stock` den Bestand zu Datum, Betrag und Konto mit der Bankreferenz je Zeile.

### 9d. BookingExports (Sätze, die in einem DATEV-Stapel standen)

Eine Zeile je Buchungssatz, der in einem Buchungsstapel stand – die Marke „exportiert“. Keine Zeile heißt: nicht exportiert (der ganze Bestand). Auch diese Zeile geht mit der Buchung (`ON DELETE CASCADE`).

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| Booking_ID | INTEGER | PRIMARY KEY, FK → Bookings ON DELETE CASCADE | der Buchungssatz |
| ExportedAt | DATETIME | NOT NULL | Zeitpunkt des ersten Exports (UTC, `CURRENT_TIMESTAMP`); ein späterer Export ändert ihn nie |

`Database.mark_datev_export` setzt das Steuerdatum und die Marke in EINER Transaktion (`DateTax` wie `update_bookings_date_tax_batch`, die Marke nur beim ersten Mal); `Database.exported_booking_ids` nennt,
welche Sätze eine Marke tragen. Die Tabelle ist nur die Marke: Sie sperrt nichts.

### 10. BookingDocuments (n:m)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Booking_ID | INTEGER | NOT NULL, FK → Bookings | |
| Document_ID | INTEGER | NOT NULL, FK → Documents | |
| RelationType | TEXT | | `receipt` (Standard) \| `invoice` \| `contract` \| `other` (`bookings.RELATION_TYPES`) |

UNIQUE(Booking_ID, Document_ID). Verknüpfbar ist jede Zeile von `Bookings`; die App verknüpft über `Database.link_receipt_to_booking`, das unter bestimmten
Bedingungen die Beleg-Nr. übernimmt (Regeln: `docs/regeln/belege.md`).

### 11. NumberRanges

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Type | TEXT | NOT NULL | `invoice`, `quote`, `receipt_company`, `receipt_category` (Rechnungen und Angebote haben je einen eigenen Kreis) |
| Year | INTEGER | NOT NULL | |
| Letter | TEXT | NOT NULL | Buchstabe (Rechnung `R`, Angebot `A`) |
| Prefix | TEXT | DEFAULT '' | |
| CurrentNumber | INTEGER | DEFAULT 0 | letzte vergebene Nummer |
| Description | TEXT | | |
| NumberFormat | TEXT | DEFAULT '{yy}{l}{nnn}{s}' | Vorlage, z. B. → `26R001` |

UNIQUE(Type, Year, Letter, Prefix). Regeln: `docs/regeln/rechnungen.md`, Abschnitt Nummern.

### 12. Invoices (Ausgangsrechnungen und Angebote)

Momentaufnahme-Prinzip: Verkäufer, Käufer und Bank werden bei der Erstellung kopiert. Die Spalten werden positionsbasiert gelesen
(`db.invoices.InvoiceCol`); neue Spalten kommen ans Ende, frische und migrierte Datenbank haben dieselbe Reihenfolge.

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| InvoiceNumber | 1 | TEXT | UNIQUE, NOT NULL | Rechnungs-/Angebotsnummer |
| InvoiceDate | 2 | DATE | NOT NULL | |
| **Verkäufer** | | | | |
| OwnCompanyId | 3 | INTEGER | FK → Contacts | |
| SellerName, SellerCompany | 4, 5 | TEXT | NOT NULL | |
| SellerStreet, SellerPostalCode, SellerCity | 6–8 | TEXT | | |
| SellerCountry | 9 | TEXT | DEFAULT 'DE' | |
| SellerVATID | 10 | TEXT | | USt-IdNr. **oder** Steuernummer (`export.einvoice_model.tax_id_kind` trennt sie) |
| SellerEmail, SellerPhone | 11, 12 | TEXT | | |
| **Käufer** | | | | |
| CustomerId | 13 | INTEGER | FK → Contacts | |
| BuyerName, BuyerCompany | 14, 15 | TEXT | NOT NULL | |
| BuyerStreet, BuyerPostalCode, BuyerCity | 16–18 | TEXT | | |
| BuyerCountry | 19 | TEXT | DEFAULT 'DE' | |
| BuyerVATID | 20 | TEXT | | USt-IdNr. des Käufers |
| BuyerReference | 21 | TEXT | | Kundenreferenz (BT-10) |
| BuyerRouteID | 22 | TEXT | | Leitweg-ID |
| **Auftrag / Dokument** | | | | |
| OrderNumber | 23 | TEXT | | |
| Currency | 24 | TEXT | DEFAULT 'EUR' | |
| DeliveryDate | 25 | DATE | | Leistungsdatum |
| **Zahlungsbedingungen** | | | | |
| PaymentTerms | 26 | TEXT | | Zahlungshinweis |
| PaymentDueDate | 27 | DATE | | Fälligkeit |
| SkontoDays | 28 | INTEGER | | Skontofrist in Tagen (1–365) |
| SkontoPercent | 29 | REAL | | Skonto in Prozent (> 0, < 100, höchstens zwei Nachkommastellen); beide leer oder 0 = kein Skonto |
| **Bank** | | | | |
| BankAccountId | 30 | INTEGER | FK → Accounts | |
| BankName, BankIBAN, BankBIC | 31–33 | TEXT | | |
| **Summen** | | | | |
| TaxCategory | 34 | TEXT | DEFAULT 'S' | |
| TaxRate | 35 | REAL | NOT NULL | Anteil (0.19); bei `MixedTaxRates = 1` nur Platzhalter (der höchste Satz) |
| SumNet, TaxAmount, SumGross | 36–38 | INTEGER | NOT NULL | Minor Units |
| AmountDue | 39 | INTEGER | NOT NULL | offener Betrag (`_recalc_invoice`) |
| **Verwaltung** | | | | |
| Status | 40 | TEXT | DEFAULT 'draft' | `draft` / `finalized` / `sent` / `overdue` / `partial_payment` / `paid` / `cancelled` (Übergänge: `db.invoice_lock.TRANSITIONS`) |
| PDFPath, XMLPath | 41, 42 | TEXT | | Ablage unter `invoices/<Jahr>/` im Datenordner des Benutzers |
| CreatedAt | 43 | DATETIME | DEFAULT CURRENT_TIMESTAMP | |
| UpdatedAt | 44 | DATETIME | | |
| **Angebot / Dokument** | | | | |
| DocumentType | 45 | TEXT | NOT NULL, DEFAULT 'invoice' | `invoice` \| `quote` |
| ValidUntil | 46 | DATE | | Angebot: gültig bis |
| IntroText, ClosingText | 47, 48 | TEXT | | Fließtext vor/nach den Positionen |
| SourceQuoteId | 49 | INTEGER | FK → Invoices ON DELETE SET NULL | Rechnung: Quell-Angebot |
| **Fassungen, Versand, Storno (Schema 2002)** | | | | |
| InvoiceKind | 50 | TEXT | | NULL = Rechnung, `storno` = Stornorechnung |
| CancelsInvoiceId | 51 | INTEGER | UNIQUE-Index `ux_invoices_cancels` (nur wenn gesetzt) | Stornorechnung: die stornierte Rechnung |
| ReplacesInvoiceId | 52 | INTEGER | | Ersatzrechnung: die ersetzte, stornierte Rechnung (höchstens eine, in der Transaktion geprüft) |
| SentAt | 53 | DATETIME | | erstmals versendet (UTC); Import und Altbestand NULL |
| DocumentVersion | 54 | INTEGER | | aktuelle Fassung; NULL = Fassung 1 |
| **Steuersatz je Position (Schema 2007)** | | | | |
| MixedTaxRates | 55 | INTEGER | | NULL = ein Satz für den ganzen Beleg (`TaxRate`; der ganze Bestand), 1 = Satz je Position. Geschrieben wird nur NULL oder 1, gelesen streng (nur die ganze Zahl 1, `has_mixed_rates`) |
| **Leistungszeitraum, Zahlungsart, Angebotsstatus (Schema 2010)** | | | | |
| ServiceDateEnd | 56 | DATE | | Ende des Leistungszeitraums; NULL = nur das Leistungsdatum (`DeliveryDate`; der ganze Bestand). Ein Zeitraum gilt nur mit zwei ISO-Daten und einem Ende nach dem Beginn, gelesen über `db.invoices.service_period`, geprüft über `normalize_service_end` |
| PaymentMeans | 57 | TEXT | | Zahlungsart, Code nach UNTDID 4461: NULL = Überweisung (58; der ganze Bestand), sonst `'10'` bar, `'48'` Karte, `'1'` nicht festgelegt. Geschrieben über `stored_payment_means` (die 58 wird NULL), gelesen streng über `payment_means_code` (alles andere gilt als nicht lesbar) |
| QuoteStatusBefore | 58 | TEXT | | Rechnung aus einem Angebot: dessen Status vor der Umwandlung (`draft`, `sent`, `accepted`); NULL = `accepted` (der ganze Bestand). `convert_quote_to_invoice` schreibt ihn, `delete_draft_invoice` liest ihn über `restored_quote_status`; kein Feld der Maske |

Der Bestand behält in den später angehängten Spalten NULL – nichts wird umgedeutet. Zu diesem Abschnitt gehören die Regeln in
**`docs/regeln/rechnungen.md`**: Dokumente und E-Rechnung (Skonto-Angabe, Pflichtangaben, Steuernummer/USt-IdNr.), mehrere Steuersätze je Beleg
(`tax_groups.rate_groups`, `split_payment`), Sperre versendeter Rechnungen (`db/invoice_lock.py`), Berichtigen (`Database.correct_invoice`,
`db/invoice_versions.py`), Statusübergänge, Nummern, Fassungen.

### 13. InvoiceItems

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| InvoiceId | 1 | INTEGER | NOT NULL, FK → Invoices ON DELETE CASCADE | |
| Position | 2 | INTEGER | NOT NULL | |
| ArticleId | 3 | INTEGER | FK → Articles | |
| Description | 4 | TEXT | NOT NULL | |
| Quantity | 5 | REAL | NOT NULL | |
| Unit | 6 | TEXT | DEFAULT 'C62' | UN/ECE-Code |
| PricePerUnit | 7 | INTEGER | NOT NULL | netto, Minor Units |
| TotalNet | 8 | INTEGER | NOT NULL | Menge × Preis, Minor Units |
| TaxCategory | 9 | TEXT | DEFAULT 'S' | `S` (Standard); `E` = steuerfrei (mit der Momentaufnahme eines steuerfreien Eintrags) |
| TaxRate | 10 | REAL | NOT NULL | Satz der Position, wie der Schreiber ihn liefert (Prozentzahl 19 oder Anteil 0,19), gelesen über `tax_groups.rate_pct` |
| **Momentaufnahme eines Steuersatzes aus `TaxRates` (Schema 2008)** | | | | |
| TaxRateBp | 11 | INTEGER | | Hundertstel Prozent; NULL = keine Momentaufnahme, der Satz steht allein in `TaxRate` (der ganze Bestand) |
| TaxExemptReason | 12 | TEXT | | Grund der Steuerbefreiung (nur bei `E`) |
| RevenueCoaId | 13 | INTEGER | | Erlöskonto des Eintrags; ohne Fremdschlüssel (frisch wie migriert), das Löschen eines Kontos prüft `coa_is_referenced`; NULL = Konto nach der bisherigen Regel |

Index `idx_invoiceitems_invoice` auf `InvoiceId`. Die Momentaufnahme steht an der Position, nicht am Kopf; Wege, die Positionen kopieren (Storno,
Ersatzrechnung, Angebot → Rechnung), nehmen sie unverändert mit (`db.invoice_payments.ITEM_SNAPSHOT_COLUMNS`).

### 14. InvoicePayments

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| InvoiceID | 1 | INTEGER | NOT NULL, FK → Invoices ON DELETE CASCADE | |
| BookingID | 2 | INTEGER | NOT NULL, FK → Bookings | Bankbewegung der Zahlung (Altbestand: auch ein Satz) |
| Amount | 3 | INTEGER | NOT NULL | das Geld, das geflossen ist, ohne Skonto. Negativ sind die Zuordnungen des WISO-Imports (Storno, Gutschrift) und die Erstattung einer Stornorechnung; sonst legt die Oberfläche nur positive an |
| PaymentDate | 4 | DATE | NOT NULL | Datum der Bankbewegung |
| Notes | 5 | TEXT | | |
| CreatedAt | 6 | DATETIME | DEFAULT CURRENT_TIMESTAMP | |
| SkontoAmount | 7 | INTEGER | | Skonto (Schema 2009): der Rest der Rechnung, den diese Zahlung als Skonto ausbucht; NULL = keines (der ganze Bestand). Nur an der Zuordnung, die die Rechnung mit Skonto schließt |

Das Paar (`InvoiceID`, `BookingID`) kommt höchstens einmal vor: UNIQUE-Index `ux_invoicepayments_invoice_booking` (neue Datenbanken gleich, bestehende
beim Öffnen – nur ohne Dubletten; sonst wird er übersprungen und die Prüfliste meldet sie). Was eine Zuordnung an der Rechnung erledigt, ist
`Amount + COALESCE(SkontoAmount, 0)` (`db.invoice_payments.settled_sql`); alles, was das Geld selbst meint, liest `Amount`. Regeln: `docs/regeln/zahlungen.md`.

### 14a. InvoiceDocumentVersions (abgelegte Fassungen)

`ID`, `InvoiceID` (NOT NULL, bewusst ohne FK), `Version` (NOT NULL), `Kind` (`pdf` \| `xml`), `RelPath` (relativ zum Ordner `invoices/` des Benutzers),
`Sha256`, `ArchivedAt` (DEFAULT CURRENT_TIMESTAMP, UTC), `Reason` (`corrected` \| `reopened`). UNIQUE(InvoiceID, Version, Kind).

### 14b. InvoiceCorrections (Protokoll)

`ID`, `InvoiceID` (NOT NULL, bewusst ohne FK), `InvoiceNumber` (Momentaufnahme), `Version` (NOT NULL), `ChangedAt` (UTC), `UserName`,
`Action` (`correct` \| `status`), `Field` (Spaltenname oder `Status`), `OldValue`, `NewValue`. Index `idx_invoicecorrections_invoice` auf `InvoiceID`.
Beide Tabellen haben keinen Fremdschlüssel, damit Protokoll und Fassungen das Löschen eines Entwurfs überleben. Aus Protokoll und `Invoices.SentAt`
ergibt sich, ob eine Rechnung schon einmal versendet war (`Database.was_sent_before`).

### 15. AssetCategories (AfA-Kategorien)

`ID`, `Name` (NOT NULL), `UsefulLifeYears` (NOT NULL), `DepreciationMethod` (DEFAULT 'linear'; `linear`, `degressive` oder `both`), `COA_ID` (FK → ChartOfAccounts,
Standard-Anlagekonto), `Notes`. Seed: 30 BMF-Kategorien.

### 16. Assets (Anlagenverzeichnis)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| InventoryNumber | TEXT | UNIQUE | automatisch `INV-YY-###` |
| Name | TEXT | NOT NULL | |
| Description | TEXT | | |
| AssetCategory_ID | INTEGER | FK → AssetCategories | |
| COA_ID | INTEGER | FK → ChartOfAccounts | Anlagekonto |
| PurchaseDate | DATE | NOT NULL | Anschaffungsdatum |
| PurchasePrice | INTEGER | NOT NULL | netto, Minor Units |
| UsefulLifeYears | INTEGER | NOT NULL | |
| DepreciationMethod | TEXT | DEFAULT 'linear' | `linear`, `degressive` |
| SerialNumber, Location | TEXT | | |
| Supplier_ID | INTEGER | FK → Contacts | |
| Document_ID | INTEGER | FK → Documents | |
| Booking_ID | INTEGER | FK → Bookings | |
| SaleDate | DATE | | Abgang |
| SalePrice | INTEGER | | Minor Units |
| Status | TEXT | DEFAULT 'active' | `active`, `sold`, `scrapped` |
| Notes | TEXT | | |
| Parent_ID | INTEGER | FK → Assets | Erweiterung (Unter-Anlage): nachträgliche Anschaffungskosten der Hauptanlage; sie zählt zu ihr, wenn die Bedingungen von `Database.counts_toward_parent` erfüllt sind |
| CreatedAt | DATETIME | DEFAULT CURRENT_TIMESTAMP | |

### 17. AssetDepreciations (AfA-Pläne)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Asset_ID | INTEGER | NOT NULL, FK → Assets ON DELETE CASCADE | |
| Year | INTEGER | NOT NULL | |
| DepreciationAmount | INTEGER | NOT NULL | AfA-Betrag, Minor Units |
| BookValue | INTEGER | NOT NULL | Restbuchwert, Minor Units |
| Booking_ID | INTEGER | FK → Bookings | die AfA-Buchung |
| Status | TEXT | DEFAULT 'planned' | `planned`, `posted` (vom Programm gebucht), `booked` (WISO-Import: der Satz steht in den Büchern, ohne `Booking_ID`) |
| BookedAt | DATETIME | | |

UNIQUE(Asset_ID, Year). „Gebucht“ heißt: die Buchung gibt es (oder `booked`). Plan (`Database.depreciation_plan`, `db/assets.py`: Formel oder
Fortschreibung), Buchen, Abgang, Erweiterungen, Löschen und der Abgleich mit dem WISO-Datenbank-Import: `docs/regeln/buchungen.md` (Abschnitt AfA) und
`docs/regeln/import.md`.

---

## Datenfluss

### Bank ↔ Satz

```
1. Tabellen-Export (WISO)  → Bookings mit BookingType='bank'
2. Original-Export (WISO)  → Bookings mit BookingType='entry' (TaxRate aus den Schlüsselregeln bzw. TaxKeys, DatevKey = SCHLUESSEL)
3. link_bank_to_entries()  → Entry.ParentBooking_ID = Bank.ID (Stufen 0–7, mit Richtungsregel)
4. Doppik-Spiegel (COA und Gegenkonto sind eigene Konten) werden in Anzeige und Editor ausgeblendet
```

Stufen, Richtungsregel, Kandidaten und die bewusst nicht verknüpften Fälle: `docs/regeln/buchungen.md` (Abschnitt Automatischer Abgleich). Die
Forderung einer Rechnung wird in keiner Stufe verknüpft (außer über die Quellklammer, Stufe 0; `matching.receivable_condition`); Stufe 7 setzt eine
Forderung auf `resolved`, wenn ihre Zahlungen sie decken (`docs/regeln/zahlungen.md`).

### Beleg lesen und Buchung vorschlagen

```
1. Upload (/receipts/upload) legt den Beleg an (Documents, Datei unter Belege/<Jahr>/) – der Upload liest nicht
2. POST /receipts/read: receipt_reader.read_receipt öffnet die Datei (receipt_files.open_receipt), liest sie in einem Kindprozess
   (receipt_reading.worker) außerhalb jeder Transaktion; E-Rechnung zuerst, dann Textregeln einer PDF; Scans und Bilder werden nicht gelesen
3. Database.save_document_reading → DocumentReadings; Documents bleibt unberührt
4. GET /receipts/suggestions?id= → Database.suggest_bookings_for_receipt (rein lesend): receipt_match_candidates + receipt_matching.rank_candidates, höchstens 5 mit festen Begründungen
5. „Zuordnen“ → POST /bookings/link-document (BookingDocuments) – der einzige Schritt, der etwas verknüpft
```

Voraussetzungen, Fenster, Bewertung: `docs/regeln/belege.md`.

### EÜR (Dashboard, `/euer`, BWA)

Die Werte werden aus `Bookings` abgeleitet (nicht aus Rechnungen): `Database.get_euer_data` liefert die Zeilen, `reporting.euer_section` ordnet je Konto zu
(Einnahme, Ausgabe, „Sonstige Ausgabe“ oder gar nicht), `db.euer.euer_statement` (Seite `/euer`, Kasten im Dashboard) und `db/bwa.py` (Seite `/bwa`)
bereiten dieselben Zahlen auf. Ob ein Satz zählt, entscheidet die Sektion in `get_euer_data` (1: Kinder einer Bankbewegung, 2/2b: eigenständige Sätze auf der
Kasse bzw. 1460, 3: nicht zahlungswirksame Umbuchungen Anlage ↔ Erfolgskonto); jeder Satz zählt in genau einer. Das Zweckkonto folgt der gespeicherten
Form. Virtuelle Konten: USt 3806, Vorsteuer 1401/1406. Das Dashboard (`Database.get_dashboard`) zerlegt jede gezählte Bewegung in vier Töpfe (Einnahme,
Ausgabe, privat, Übertrag; `reporting.split_bank_movement`). Regeln: `docs/regeln/auswertungen.md`.

### Summen- und Saldenliste (`db/susa.py`, `Database.get_susa`, Seite `/susa`)

Je Konto Anfangssaldo, Soll/Haben im Monat und aufgelaufen, Endsaldo. Liest dieselben Zeilen (`accounts.LEDGER_ROWS_WHERE`) und dieselbe Lesart von Form,
Soll und Haben wie der DATEV-Export, rein lesend. Regeln: `docs/regeln/auswertungen.md`, Abschnitt Summen- und Saldenliste.

### Liquiditätsrechnung (`db/liquidity.py`, `Database.get_liquidity`, Seite `/liquiditaet`)

Überleitung vom BWA-Ergebnis zur Veränderung der flüssigen Mittel; keine eigene Lesart der Buchungen, nur `get_euer_data`, `bwa.build_report` und die
Dashboard-Töpfe; „Nicht zugeordnet“ ist die Differenz. Rein lesend. Regeln: `docs/regeln/auswertungen.md`, Abschnitt Liquiditätsrechnung.

### Rechnungserstellung

```
1. NumberRanges → InvoiceNumber
2. Contacts (ContactType='own') → Verkäufer-Momentaufnahme; Contacts (Kunde) → Käufer-Momentaufnahme; Accounts → Bank-Momentaufnahme
3. Articles / TaxRates → InvoiceItems
4. Status: draft → finalized → sent → paid (dazwischen partial_payment, overdue; cancelled)
5. Versenden bucht die Forderung; Zahlungen lassen sich ab sent zuordnen (9a)
6. Stornieren: Stornorechnung als eigener Beleg (9b); Erstatten: ausgehende Bankbewegung an der Stornorechnung zuordnen
7. Dokumente (PDF mit CII, UBL): das Leistungsdatum oder – mit `Invoices.ServiceDateEnd` – der Leistungszeitraum (PDF-Kopf in zwei Zeilen, CII `BillingSpecifiedPeriod`, UBL
   `cac:InvoicePeriod`); die Zahlungsart `Invoices.PaymentMeans` (NULL = Überweisung 58 mit IBAN, sonst allein der Code); nur diese Stellen lesen die beiden Spalten
   (`service_period`, `payment_means_code`)
```

### AfA-Berechnung

```
1. AssetCategories → Nutzungsdauer + Methode;  2. Assets → Erfassung
3. AssetDepreciations → Plan (Formel `calculate_depreciation_plan`, Fortschreibung nach gebuchten Jahren in `depreciation_plan`)
4. book_depreciation legt je Jahr genau eine Umbuchung „Aufwand an Anlagekonto“ an (positiv, ohne Account_ID, 31.12.), Sperre gegen Doppelbuchung in derselben Transaktion
5. EÜR: Sektion 3; DATEV-Export: „Aufwand an Anlagekonto“, BU 0
```

Das Anlagekonto kommt aus dem Formular, sonst aus der Anlage oder ihrer Kategorie; abgeschrieben wird direkt (Gegenkonto = Anlagekonto). Die zeitanteilige AfA
im Abgangsjahr plant die App noch nicht (`docs/aufgaben.md`). Ein Aufwand, der schon auf 6260 (Sofortabschreibung) gebucht ist, wird bei zusätzlicher AfA
doppelt gezählt – das prüft das Programm nicht. Regeln: `docs/regeln/buchungen.md`.

### DATEV-Export (`export/datev.py`, Regeln in `db/datev_keys.py`)

Der Stapel (EXTF 700) bildet nach, was ein Buchhaltungsprogramm für Steuerbüros schreibt; ob die Kanzlei ihn so annimmt, klärt das Steuerbüro. Soll/Haben folgt
der Form des Satzes. **BU-Schlüssel** (Spalte 8):

```
1. Bookings.DatevKey gesetzt (Import, 1–9999)  → unverändert ausgeben
2. sonst ableiten nach Art des Zweckkontos und Satz:
     Erlöskonto (4000–4999 und 6884–6892) → Umsatzsteuer 101 (19 %) / 102 (7 %) / 5 (16 %) / 4 (5 %)
     anderes Zweckkonto → Vorsteuer 401 / 402 / 7 / 6;  Debitor, Spiegel, kein/anderer Satz → 0
   (2020-07-01 bis 2020-12-31 tauschen die Plätze: 16 %/5 % = Regelsatz/ermäßigt)
3. Rechnungskette (Forderung, Wartekonto an Erlöskonto, Stornogegenbuchung) → Schlüssel 0, der Satz steht in Spalte 40
```

Rechnungsdaten (Leistungsdatum – bei einem Leistungszeitraum dessen Ende –, Land) liefert `get_datev_invoice_data` in einer Abfrage, die Wartekonten `OPEN_REVENUE_ACCOUNTS`/`get_waiting_account_numbers`;
Berater- und Mandantennummer kommen aus `CompanyDetails`. Regeln zu Zeitraum, Prüfungen, Zusammenlegen der Überträge zwischen eigenen Konten, Hinweisen
und Bestätigung (`db.matching.settle_transfers`, `Database.check_transfers`, `find_unbooked_bank_movements`, `handlers.handle_datev_export`):
`docs/regeln/auswertungen.md`, Abschnitt DATEV-Export. **Diese Zählregel nicht lockern – lieber ein Hinweis zu viel.**

#### Grenzen der Übertragserkennung

Die Erkennung (Gruppen je Ausgangskonto, Eingangskonto und Betrag; Abschnitte bei Lücken über `TRANSFER_DAY_WINDOW` = 7 Tage; Umfeld ± `TRANSFER_NEIGHBOUR_DAYS`
= 14 Tage) zählt und rät im Zweifel nicht; jede Zeile bleibt dann stehen. Belegt durch `tests/test_datev_transfer_enumeration.py` (alle kleinen Bestände,
vollständige Kontoauszüge vorausgesetzt): Bis zu 14 Tagen Verzögerung zwischen den beiden Seiten geht kein Übertrag verloren, und jede Doppelung zwischen
Spiegel-Sätzen wird genannt. Darüber hinaus gelten diese Grenzen, jede mit einem benannten Test in `tests/test_datev_transfer_hints.py` (`test_known_limit_…`):

- *Laufzeit über 14 Tage:* Ein Übertrag kann doppelt im Stapel stehen, ohne dass der Export es sagt, und – selten – in einem Zeitraum fehlen (meist meldet der
  erste Hinweis dann die Bewegung ohne Satz). Liegen die beiden Seiten weiter als das Fenster auseinander, meldet der erste Hinweis die satzlose Seite, obwohl
  der Übertrag im Stapel steht (Falschalarm).
- *Eine weit entfernte Lücke sperrt eine ganze Serie:* Fehlt in einem lückenlosen Dauerauftrag (alle Abstände höchstens sieben Tage), auch Monate davor, der Satz einer
  Seite, ist die Reihe ein einziger Abschnitt, und der Ausgang ohne Satz sperrt ihn ganz – nichts fällt weg, die Eingangs-Sätze stehen doppelt und werden genannt
  (dritter Hinweis, mit dem Auslöser im Umfeld).
- *Stückelung:* Eine Sammelüberweisung mit anderer Stückelung (A −200 mit einem Spiegel über 200, auf B zwei Gutschriften über je 100 mit je einem Spiegel, oder −100 gegen
  zweimal 50 neben einer Lieferantenzahlung über 50) bildet verschiedene Gruppen: Der Stapel trägt die Buchung doppelt, und niemand sagt es.
- *Konten mit gleicher SKR-Nummer:* Keins von beiden nimmt teil; ein beidseitig kontierter Übertrag zwischen einem von ihnen und einem anderen Konto steht mit beiden Sätzen
  im Stapel, ohne Hinweis.
- *Ungemeldet* bleibt außerdem ein ganz unkontierter Übertrag zwischen zwei halb kontierten Überträgen gleichen Betrags, deren kontierte Seite jenseits des Fensters
  von der satzlosen liegt (drei Überträge gleicher Höhe in wenigen Wochen; deren satzlose Seiten werden dann gemeldet), und neben einem Übertrag, dessen beide
  Bankbewegungen anders kontiert sind und zu dem ein eigenständiger Spiegel steht (drei Erfassungen desselben Übertrags).
- *Ein eigenständiger Spiegel neben einer anders kontierten Bankbewegung* wird nicht genannt (nur neben einem Bein mit Spiegel).
- *Ein Text als Betrag auf einer Bankbewegung mit Sätzen im Zeitraum* lässt die Prüfung der Splits (`find_unbalanced_splits`, vor den Hinweisen) mit einer Fehlermeldung
  scheitern – dieser Weg ist nicht Teil der Zählregel.

Die Deckung der Dashboard-Karte (`settle_transfers`, über den ganzen Bestand) hat dieselben Grenzen; ihre weiteren Grenzen stehen in `docs/regeln/auswertungen.md`
(Abschnitt Dashboard). Sauberer wäre, die beiden Bankbewegungen eines Übertrags ausdrücklich zu verknüpfen (`docs/aufgaben.md`).

---

## Besonderheiten

1. **Foreign Keys:** `PRAGMA foreign_keys = ON`.
2. **ON DELETE CASCADE:** `InvoiceItems`, `InvoicePayments`, `AssetDepreciations`, `ContactAddresses`, `CompanyDetails`, `PersonDetails`, `DocumentReadings`
   (nicht `InvoiceCorrections`, `InvoiceDocumentVersions`). `WorkTimes.PersonID` und `Trips.DriverID` tragen im Schema CASCADE, `WorkTimes.CustomerID` und
   `Trips.DocumentID` SET NULL – die App lässt es dazu nicht kommen: Kontakt, Artikel, Bankkonto, AfA-Kategorie und Beleg werden nur gelöscht, solange nichts auf
   sie verweist (`db.DeleteRefused`; `docs/regeln/buchungen.md`); die Lesung eines Belegs ist kein solcher Verweis.
3. **Momentaufnahme:** Rechnungen kopieren Stammdaten bei der Erstellung; Positionen kopieren den gewählten Steuersatz.
4. **Datum:** ISO 8601 (`YYYY-MM-DD`). **Steuersätze:** Anteil (0.19) in `Bookings`/`DocumentReadings`/`Invoices`, Prozent in `Articles`, Hundertstel Prozent in `TaxRates`/`InvoiceItems.TaxRateBp`.
5. **Geldbeträge:** ganze Zahlen in Minor Units (`money.py`, kaufmännisch gerundet); Mengen und Prozentwerte sind `REAL`.
6. **Seeding:** `_seed_chart_of_accounts`, `_seed_asset_categories`, `_seed_tax_keys` laden aus `seed_data/` (nur in leere Tabellen).
7. **Migration:** vor jedem Lauf einer bestehenden Datenbank eine Sicherung nach `backup/` neben der Datenbank, die Umstellung in EINER Transaktion, eine neuere
   Datenbank wird nie geöffnet (`SchemaTooNewError`); Stufen und Regeln: `docs/regeln/versionierung.md`.
