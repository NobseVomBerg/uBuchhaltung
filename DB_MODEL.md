# Datenbankmodell uBuchhaltung

**Stand:** Version 2.6.0 (Schema 2006)
**DBMS:** SQLite 3
**Datei:** `data/users/<Name>/buch.db`

> WISO, WISO Mein Büro und DATEV sind Marken ihrer jeweiligen Inhaber. uBuchhaltung ist ein
> unabhängiges Projekt und steht in keiner Verbindung zu den Markeninhabern oder den Herstellern.
> Die Namen dienen allein der Bezeichnung der unterstützten Import- und Exportformate.

---

## Übersicht

uBuchhaltung verwaltet Buchführung für kleine/mittlere Unternehmen mit Fokus auf deutsche Buchhaltungsstandards (SKR03/SKR04). Es unterstützt die Erzeugung von XRechnung und ZUGFeRD (EN 16931), Anlagenverwaltung (AfA), automatische Bank↔Entry-Verknüpfung und Belegmanagement.

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
│ • ContactTypeLinks  │ Kontakttypen-Mehrfachzuordnung (n:m)
│ • PersonRoles       │ Personen-Rollen (n:m)
│ • Articles          │ Artikel-/Dienstleistungskatalog
│ • Categories        │ Kategorien für Privatbelege
│ • AssetCategories   │ AfA-Kategorien nach BMF
│ • TaxKeys           │ DATEV-Steuerschlüssel (BU-Codes)
└─────────────────────┘

┌─────────────────────┐
│ GESCHÄFTSVORFÄLLE   │
├─────────────────────┤
│ • Bookings          │ Buchungssätze (bank + entry)
│ • Documents         │ Belege/Dokumente
│ • DocumentReadings  │ Lesung eines Belegs (aus der Datei gelesene Felder)
│ • BookingDocuments  │ n:m Verknüpfung Buchungen↔Belege
└─────────────────────┘

┌─────────────────────┐
│ RECHNUNGSWESEN      │
├─────────────────────┤
│ • NumberRanges      │ Nummernkreise
│ • Invoices          │ Ausgangsrechnungen (XRechnung) UND Angebote
│                     │ (DocumentType='invoice'|'quote'; SourceQuoteId
│                     │ verweist bei umgewandelten Rechnungen aufs Angebot)
│ • InvoiceItems      │ Rechnungs-/Angebotspositionen
│ • InvoicePayments   │ Zahlungsverknüpfungen
│ • InvoiceCorrections│ Protokoll: Berichtigungen und Statuswechsel
│ • InvoiceDocument-  │ abgelegte Fassungen (PDF/XML) einer Rechnung
│   Versions          │
└─────────────────────┘

┌─────────────────────┐
│ ZEITERFASSUNG       │
├─────────────────────┤
│ • WorkTimes         │ Arbeitszeiten je Person (Arbeit/Urlaub/Krank/Feiertag)
│ • Trips             │ Fahrtenbuch (Fahrer, Start/Ziel, km, Belegverknüpfung)
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

Beim Erstellen der Datenbank werden folgende Tabellen automatisch befüllt (nur wenn leer). Standardkonten, die eine neuere
Version mitbringt und die einem bestehenden Bestand fehlen, legt nur die Aktion „Standardkonten ergänzen“ auf der Seite des
Kontenrahmens an (`Database.add_missing_standard_accounts`) – nie der Start:

| Tabelle | Quelle | Einträge |
|---------|--------|----------|
| ChartOfAccounts | `seed_data/chart_of_accounts_skr04.json` + optional `seed_data/private/` | 88 Standard + eigene |
| AssetCategories | `seed_data/asset_categories.json` | 30 BMF-Kategorien |
| TaxKeys | `seed_data/tax_keys.json` | 53 DATEV-BU-Schlüssel |

### Testdaten (`seed_data/test/`)

Nur über `load_test_seed_data()` geladen: vom Demo-Modus (ohne die Rechnungen und, mit `include_dated=False`, ohne die fest datierten Buchungen, Anlagen, Angebote und Arbeitszeiten – die erzeugt `demo_history.py` relativ zum Startdatum) und von den Tests. Die Oberfläche bietet das Laden seit 2.3.9 nicht mehr an – Beispieldaten gibt es im Demo-Modus. Die Funktion ist idempotent (kein Doppel-Einfügen):

| Datei | Inhalt |
|-------|--------|
| `test_accounts.json` | Bankkonten |
| `test_articles.json` | 50 Artikel (Tante-Emma-Laden, 7 %/19 % MwSt) |
| `test_assets.json` | 22 Anlagegüter (PKW, Werkbänke, Schränke, PC, 3D-Drucker) |
| `test_bookings.json` | Buchungen |
| `test_contacts.json` | 33 Kontakte (1 eigene Firma, 31 Kunden, 1 Lieferant) |
| `test_documents.json` | Belege |
| `test_invoices.json` | 50 Rechnungen (2024–2026, diverse Status) |

`seed_data/private/` bleibt leer im Repository (in `.gitignore`).

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
| PrivateSharePercent | INTEGER | DEFAULT 0 | Privatanteil in % (0–100); wird gepflegt und angezeigt, aber in keiner Auswertung (EÜR, Dashboard, DATEV-Export, Abgleich) berücksichtigt |
| ShowInMenu | INTEGER | DEFAULT 1 | 1=im Auswahlmenü sichtbar, 0=ausgeblendet |

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
| BusinessOnly | INTEGER | NOT NULL, DEFAULT 1 | Reines Geschäftskonto (2.5.0, Schema 2005, letzte Spalte): 1 = reines Geschäftskonto (Kontoauszüge werden vollständig importiert), 0 = gemischt genutzt (beim Import lassen sich einzelne Umsätze auswählen). Streng gelesen (`db.accounts.is_business_only`, `Database.account_allows_selection`): nur die ganze Zahl 0 heißt „gemischt“, jeder andere gespeicherte Wert gilt als reines Geschäftskonto, die Kasse nie als gemischt. Der Bestand bekam bei der Umstellung überall 1. **Ändert keine Buchung und keine Auswertung** |

**Business Logic:**
- Das Kennzeichen `BusinessOnly` steuert allein die Oberfläche des Kontoauszug-Imports: Nur bei einem gemischt genutzten Konto
  zeigt die Vorschau Kästchen je Umsatz, und `/confirm_transactions` nimmt dann das Feld `selection` an (sonst 400); gepflegt wird
  es im Formular Stammdaten → Bankkonten (Kästchen „Reines Geschäftskonto“, Standard angehakt; nicht bei der Kasse)
- Konto "Kasse" wird automatisch beim Start erstellt, neu angelegt mit `SKRAccount` 1600
  (SKR04 „Kasse“, bei Bedarf in den Kontenrahmen nachgelegt). Eine vorhandene Kasse
  bleibt unverändert – mit oder ohne SKR-Nummer, auch mit 1460 (Verrechnungskonto)
- `SKRAccount` verknüpft mit ChartOfAccounts.AccountNumber für Doppik-Erkennung
- Wird von `_get_bank_coa_ids()` genutzt um echte Bankkonten zu identifizieren; die Kasse
  zählt dort wie ein Bankkonto mit, sobald sie eine SKR-Nummer trägt (die Standard-Kasse: 1600)
- `get_payment_coa_ids()` ergänzt diese Menge um das Verrechnungskonto 1460 (Geldtransit;
  `importers.wiso_fdb.DEFAULT_LIQUID_ACCOUNTS`). Der SKR03-Kassenbereich 1000–1099 gehört seit
  2.1.0 nicht mehr dazu (in SKR04 sind es Vorräte); die Migration auf 2.1.0 hat ihn für den
  Bestand festgeschrieben (`Bookings.EntryForm`). Die Kasse der App zählt über ihr eigenes
  Konto: Hat sie keine SKR-Nummer (ältere Datenbank), ist 1600 kein Zahlungskonto. Die Menge
  bestimmt, wo die Importe das Vorzeichen setzen, welche Form ein neuer Satz bekommt
  (`derive_entry_form`) und wie eine Zeile ohne Form gelesen wird; Verknüpfung und
  Split-Summe (`is_bank_effective`) rechnen mit den eigenen Konten

---

### 3. Contacts – normalisiertes Schema (6 Tabellen)

#### 3a. Contacts (Basis)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| ContactType | TEXT | DEFAULT 'customer' | 'customer', 'supplier', 'own', 'insurance', 'other' |
| EntityType | TEXT | DEFAULT 'company' | 'company' oder 'person' |
| DisplayName | TEXT | | Anzeigename |
| CustomerNumber | TEXT | UNIQUE | Kundennummer |
| Abbreviation | TEXT | UNIQUE | Kürzel (2–3 Buchstaben, wird aus dem Namen vorgeschlagen) |
| Email | TEXT | | E-Mail |
| Phone | TEXT | | Telefon |
| Notes | TEXT | | Notizen |
| Logo | TEXT | | Pfad zum Firmenlogo |
| VatMethod | TEXT | | Besteuerungsart der Umsatzsteuer (2.4.0, Schema 2004): `'ist'` (Ist-Versteuerung, vereinnahmte Entgelte) oder `'soll'` (Soll-Versteuerung, vereinbarte Entgelte); NULL = nicht festgelegt, jeder andere Wert gilt als nicht festgelegt (`db.contacts.vat_method`). Nur ein eigener Kontakt (`ContactType='own'`, Firma oder Person) trägt sie (`Database.get_vat_method`: der erste mit gültigem Wert). **Wird nur gespeichert und angezeigt** (Formular des eigenen Kontakts, Hinweis auf *Sonstiges*): Keine Buchung, EÜR, Dashboard und kein Export liest sie – Erlös und Umsatzsteuer entstehen weiter mit der Zahlung (Abschnitt 9a) |

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
| DatevConsultantNo | TEXT | | Beraternummer für den Vorlauf des DATEV-Stapels (2.3.0, Schema 2003): eine Zahl von 1001 bis 9999999, nur ASCII-Ziffern ohne führende Null; NULL = nicht eingetragen. Nur die eigene Firma (`ContactType='own'`) trägt sie (`Database.get_datev_numbers`); der WISO-Datenbank-Import füllt sie, wenn beide Felder leer sind (erst nach den Buchungen) |
| DatevClientNo | TEXT | | Mandantennummer: eine Zahl von 1 bis 99999, nur ASCII-Ziffern ohne führende Null; NULL = nicht eingetragen |

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
| JobTitle | TEXT | | Position / Jobtitel |
| Department | TEXT | | Abteilung |
| IsPrimaryContact | INTEGER | DEFAULT 0 | 1 = Hauptansprechpartner der Firma |

#### 3e. ContactTypeLinks (n:m)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ContactID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | |
| TypeKey | TEXT | NOT NULL | Kontakttyp; mehrere je Kontakt möglich (`own` ist ein Systemtyp und steht nur in `Contacts.ContactType`) |

**Indizes:** PRIMARY KEY (ContactID, TypeKey)

#### 3f. PersonRoles (n:m)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ContactID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | |
| RoleKey | TEXT | NOT NULL | Fachliche Rolle einer Person, z. B. `invoice_recipient`, `accounting` |

**Indizes:** PRIMARY KEY (ContactID, RoleKey)

---

### 4. Articles (Artikelstamm)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Name | TEXT | NOT NULL | Artikelbezeichnung |
| Unit | TEXT | DEFAULT 'Stk.' | Einheit |
| UnitPrice | INTEGER | DEFAULT 0 | Netto-Einzelpreis in Minor Units |
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

**Zweck:** BU-Schlüssel (Code → Steuersatz) für den WISO-Textimport. Wird aus `seed_data/tax_keys.json` geseeded (53 Einträge); Schema 2003
berichtigt und ergänzt in bestehenden Datenbanken 100, 120, 121 („Verwendung von Gegenständen 19 %“, vorher fälschlich „steuerfrei“), 122 und 490
(ohne Satz) – `schema.TAX_KEY_CORRECTIONS`. Der Export schlüsselt nicht aus dieser Tabelle, sondern aus `db/datev_keys.py`.

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| Code | TEXT | PRIMARY KEY | BU-Schlüssel (z.B. '9', '401') |
| Description | TEXT | NOT NULL | Beschreibung |
| TaxRate | REAL | | Steuersatz als Dezimal (0.19 = 19%), NULL = kein Satz |
| TaxType | TEXT | | 'USt', 'VSt', 'steuerfrei', '§13b', 'keine', 'UStfrei' |

**Verwendung:**
- WISO-Original-Import: SCHLUESSEL → zuerst die Regeln von `db/datev_keys.py` (`key_rate`: 401/101/121 Regelsatz, 402/102/122 ermäßigt,
  7/6/5/4 die anderen Sätze, im zweiten Halbjahr 2020 mit 16/5/19/7 %), sonst `TaxKeys` → TaxRate
- TaxAmount-Berechnung: `|Brutto| - |Brutto| / (1 + Rate)`

---

### 7. Documents (Belege)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Number | TEXT | | Belegnummer (nicht eindeutig; Nummernkreis `receipt_company`) |
| Date | DATE | | Belegdatum |
| Filename | TEXT | | Dateiname |
| Path | TEXT | | Pfad der Datei relativ zum Datenordner des Benutzers (`Belege/<Jahr>/<Nummer>_<Name>`, vom Server gesetzt); gilt nur innerhalb von `Belege/` und `Documents/` (Bestand des früheren Uploads) |
| Info | TEXT | | Zusatzinformationen |

### 7a. DocumentReadings (Lesung eines Belegs, seit 2.6.0)

**Zweck:** Die aus der Datei eines Belegs gelesenen Felder (`receipt_reading/`, `server/receipt_reader.py`), getrennt von dem, was der Nutzer am
Beleg pflegt (`Documents`). Eine Lesung ist eine Lesung der Datei, nicht der Inhalt des Belegs: abgeleitete Daten, die sich jederzeit neu lesen
lassen. Kein Wert geht von hier in `Documents`, `Bookings` oder anderswohin – die Seite zeigt sie mit ihrer Herkunft, übernommen wird nichts ohne Klick.

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| Document_ID | INTEGER | NOT NULL, UNIQUE, FK → Documents ON DELETE CASCADE | der Beleg; je Beleg höchstens eine Lesung (eine neue ersetzt die vorige) |
| Status | TEXT | NOT NULL | `ok` (mindestens ein Feld gelesen) \| `empty` (lesbares Dokument ohne lesbares Feld) \| `no_text` (Bild oder Scan: kein Text, keine Texterkennung) \| `unsupported` (anderer Dateityp, keine unterstützte E-Rechnung, Kontoauszug, PDF mit Passwort) \| `too_large` (zu groß oder Zeitlimit) \| `failed` (nicht lesbar, keine brauchbare Ausgabe des Lesers, Beleg ohne Datei) |
| Source | TEXT | | Herkunft der Felder: `einvoice_cii` \| `einvoice_ubl` (E-Rechnung) \| `text` (Textregeln); NULL, wo nichts gelesen wurde |
| ReaderVersion | INTEGER | NOT NULL | Stand der Leseregeln (`receipt_reading.READER_VERSION`); eine Lesung mit älterem Stand gilt als ungelesen |
| ReadAt | DATETIME | NOT NULL | Zeitpunkt der Lesung, UTC (`JJJJ-MM-TT HH:MM:SS`) |
| DocDate | DATE | | gelesenes Belegdatum (ISO) – nicht `Documents.Date` |
| GrossMinor | INTEGER | | gelesener Bruttobetrag in Minor Units (1 = 0,0001 EUR, `money.SCALE`), wie das Dokument ihn nennt (eine Gutschrift trägt positive Beträge) |
| NetMinor | INTEGER | | gelesener Nettobetrag, Minor Units |
| TaxMinor | INTEGER | | gelesene Steuer, Minor Units |
| TaxRate | REAL | | gelesener Steuersatz als Anteil (0.19, wie in `Bookings`) – nur, wenn der Beleg genau einen nennt |
| Currency | TEXT | | gelesene Währung (drei Großbuchstaben); die Textregeln lesen nur `EUR` |
| SenderName | TEXT | | Absender (höchstens 200 Zeichen) |
| SenderVatId | TEXT | | USt-IdNr. des Absenders (höchstens 20 Zeichen) |
| InvoiceNumber | TEXT | | Rechnungsnummer des Belegs (höchstens 100 Zeichen) |
| Kind | TEXT | | `invoice` \| `credit_note` |
| Raw | TEXT | | JSON, höchstens 64 KB: `fields` (je gelesenem Feld `value`, `source` = `einvoice`, `rule` oder `default`, dazu bei der E-Rechnung der BT-Bezeichner `ref`, bei den Textregeln die Zeile `quote` und die Seite `page`), `checks` (Summenprobe `sum`, Satzprobe `rate`: `ok` \| `mismatch` \| `incomplete`), `tax_breakdown` (die Steueraufschlüsselung) und `notes` (die Befunde als feste Schlüssel, siehe Kopf von `receipt_reading/__init__.py`) |
| Text | TEXT | | der aus der Datei gelesene Text (PDF; höchstens 20 000 Zeichen, bei längerem nur der Anfang) – Eingabe für die Namenswörter der Vorschläge, auf keiner Seite angezeigt |

**Indizes:** `idx_documentreadings_gross` auf `GrossMinor`; `Document_ID` ist UNIQUE. Es gibt keine CHECK-Regeln: Was ein Leser nicht erkennt,
bleibt NULL, und was nicht streng passt (Datum, Betrag, Währung), wird nie geraten.

**Schreiben und Lesen:** Geschrieben wird allein über `Database.save_document_reading(document_id, expected_path, row)` (`db/document_readings.py`):
Prüfung und Schreiben in EINER `BEGIN IMMEDIATE`-Transaktion – gespeichert wird nur, wenn der Beleg noch da ist und `Documents.Path` noch der Pfad ist,
den der Leser gelesen hat; die vorhandene Lesung wird ersetzt. Das Lesen der Datei selbst läuft davor außerhalb jeder Transaktion in einem Kindprozess.
Die Liste der Belege liest je Beleg nur Status, Brutto, Währung, Absender und Version (`document_reading_summaries`, nie `Text` oder `Raw`).

**Löschen:** Die Lesung hängt mit `ON DELETE CASCADE` am Beleg und geht mit ihm; `delete_receipt` löscht sie in derselben Transaktion. Sie zählt
nicht als Verweis (`DeleteRefused` nennt sie nie), nach einer Ablehnung bleibt sie stehen. Ihre Schreibwege gehen nicht ins SQL-Protokoll
(`_log_sql`): abgeleitete Daten. Die Vorlage „Zeitbereich löschen“ der SQL-Konsole löscht die Lesungen der Belege des Zeitraums mit.

**Bestand:** Die Migration auf 2.6.0 legt die Tabelle leer an und ändert keine vorhandene – ein Beleg mit Datei gilt als ungelesen, bis er gelesen
wird. Zum Datenfluss der Vorschläge siehe „Beleg lesen und Buchung vorschlagen“ unten.

---

### 8. WorkTimes (Arbeitszeiten)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| PersonID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | Person (Kontakt, z. B. Mitarbeiter oder eigene Daten) |
| Date | DATE | NOT NULL | Datum |
| Kind | TEXT | NOT NULL, DEFAULT 'work' | 'work', 'vacation', 'sick', 'holiday' |
| CustomerID | INTEGER | FK → Contacts ON DELETE SET NULL | Kunde (optional) |
| StartTime | TEXT | | 'HH:MM' (beim Speichern geprüft: Stunde 0–23, Minute 0–59) |
| EndTime | TEXT | | 'HH:MM'; liegt sie vor der Startzeit, endet der Eintrag am Folgetag (zählt zum Datum des Eintrags) |
| PauseMinutes | INTEGER | DEFAULT 0 | Pause in Minuten |
| LocationMode | TEXT | DEFAULT 'customer' | 'own', 'customer', 'other' |
| LocationCity | TEXT | | Aufgelöster Einsatzort (Momentaufnahme) |
| Note | TEXT | | Notiz |
| PauseText | TEXT | | Pausenzeiten als Text, z. B. '12:00-12:30' |

**Indizes:** `idx_worktimes_person_date` auf (PersonID, Date)

### 8a. Trips (Fahrtenbuch)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| DriverID | INTEGER | NOT NULL, FK → Contacts ON DELETE CASCADE | Fahrer |
| StartDate | DATE | NOT NULL | Datum des Fahrtbeginns |
| StartTime | TEXT | | optional, 'HH:MM' |
| EndDate | DATE | | optional |
| EndTime | TEXT | | optional, 'HH:MM' |
| StartPoint | TEXT | | Startpunkt; leer = eigene Adresse / Firmensitz |
| Destination | TEXT | NOT NULL | Ziel |
| Vehicle | TEXT | | Fahrzeug (Freitext) |
| Reason | TEXT | | Grund der Fahrt |
| DistanceKm | INTEGER | | Gefahrene Kilometer (manuell oder berechnet) |
| StartKm | INTEGER | | Tachostand Start (optional) |
| EndKm | INTEGER | | Tachostand Ende (optional) |
| DocumentID | INTEGER | FK → Documents ON DELETE SET NULL | Verknüpfter Beleg (optional) |
| CreatedAt | DATETIME | DEFAULT NOW | |

**Indizes:** `idx_trips_driver_date` auf (DriverID, StartDate)

**Prüfung beim Speichern** (`handlers._trip_fields`, seit 2.3.12): Fahrer, Start-Datum (ISO) und Ziel sind Pflicht; das
End-Datum ist leer oder ein ISO-Datum nicht vor dem Start; Uhrzeiten leer oder `HH:MM`; `StartKm`, `EndKm`, `DistanceKm` leer
oder ganze Zahlen von 0 bis 9 999 999; `EndKm` nie unter `StartKm` (sind beide gesetzt, ist `DistanceKm` die Differenz).
Ein Anschluss an die Vorfahrt wird beim Speichern nie verlangt. `insert_trip` liefert bei einem Fehlschlag `None`,
`update_trip` `False` – die Seite zeigt es. Bestehende Zeilen werden nicht nachträglich geprüft.

**Lückenprüfung** (`db.trips.logbook_gaps`, `Database.trip_logbook_check(vehicle, year)`; rein lesend, nur auf Anfrage):
alle Fahrten eines Fahrzeugs (Vergleich getrimmt und ohne Groß-/Kleinschreibung, über alle Fahrer) mit `StartDate` im
Kalenderjahr, sortiert nach Datum, Uhrzeit, ID. Eine Lücke ist jede Fahrt mit `StartKm`, deren nächster Vorgänger mit
`EndKm` einen anderen Stand hat (Differenz positiv: Kilometer fehlen, negativ: Überschneidung). Fahrten ganz ohne
Tachostand dazwischen werden gezählt, nicht als Lücke gemeldet; nach einer Fahrt mit `StartKm`, aber ohne `EndKm`, ist
der Stand unbekannt, die nächste Fahrt hat keinen Vorgänger. Gemeldet werden höchstens 100 Lücken und die wahre Zahl.

---

### 9. Bookings (Buchungssätze)

**Zweck:** Zentrale Tabelle für Bank-Bewegungen und Buchungssätze. Über `BookingType` und `ParentBooking_ID` werden Bankbewegungen mit ihren Buchungssätzen verknüpft.

| Spalte | Idx | Typ | Constraints | Beschreibung |
|--------|-----|-----|-------------|--------------|
| ID | 0 | INTEGER | PRIMARY KEY | Auto-Increment |
| DateBooking | 1 | DATE | NOT NULL | Buchungsdatum |
| DateTax | 2 | DATE | | Steuerdatum |
| BookingGroup_ID | 3 | INTEGER | – | stillgelegt (Schema v5); Splits laufen über ParentBooking_ID |
| Account_ID | 4 | INTEGER | FK → Accounts | Eigenes Bank-/Kassenkonto |
| ForeignBankAccount | 5 | TEXT | | Fremde IBAN/Kontonummer |
| RecipientClient | 6 | TEXT | | Empfänger/Auftraggeber |
| Contact_ID | 7 | INTEGER | FK → Contacts | Kontakt-Zuordnung |
| COA_ID | 8 | INTEGER | FK → ChartOfAccounts | SKR-Sollkonto |
| CounterCOA_ID | 9 | INTEGER | FK → ChartOfAccounts | SKR-Gegenkonto (Doppik) |
| Category_ID | 10 | INTEGER | FK → Categories | Kategorie |
| Amount | 11 | INTEGER | NOT NULL | Betrag in Minor Units; bei Zahlungen die Richtung der Geldbewegung (Einnahme positiv), bei Umbuchungen positiv = `COA_ID` im Soll (siehe „Form eines Buchungssatzes“) |
| Currency | 12 | TEXT | DEFAULT 'EUR' | Währung |
| TaxRate | 13 | REAL | | Steuersatz (0.19 = 19%) |
| TaxAmount | 14 | INTEGER | | Berechneter Steuerbetrag in Minor Units |
| Text | 15 | TEXT | | Verwendungszweck |
| DocumentNumber | 16 | TEXT | | Belegnummer |
| BookingType | 17 | TEXT | DEFAULT 'entry' | **'bank'** = Bankbewegung, **'entry'** = Buchungssatz |
| ParentBooking_ID | 18 | INTEGER | FK → Bookings(ID) | Verknüpfung: Entry → Bank-Buchung |
| Status | 19 | TEXT | | Frei definierbar; belegt sind `'resolved'` (Debitoren-Entry aufgelöst), `'posted'` (AfA-Buchung) und `'invoice_payment'` (Satz einer Rechnungszahlung, siehe 9a) |
| AutoMirror | 20 | INTEGER | DEFAULT 0 | 1 = die App hat den Satz selbst als Spiegel der Bankbewegung angelegt (wird beim Bearbeiten der Bankzeile mitgezogen) |
| SourceGroup | 21 | TEXT | | Klammer aus dem Quellsystem (WISO `ACCOUNTINGID`). Legt WISO die Buchungen neu an (neue `ACCOUNTINGID`, sonst gleicher Inhalt), hängt der Re-Import die vorhandene Zeile auf die neue Gruppe um – nur diese Spalte (seit 2.3.2, siehe *WISO-Datenbank-Import*) |
| EntryForm | 22 | TEXT | | Form des Buchungssatzes (siehe unten): `purpose_first`, `liquid_first`, `transfer`, `mirror_coa`, `mirror_counter`, `mirror_legacy`. Bankzeilen haben keine (NULL). **Immer am Ende angefügt** – die Leser lesen `Bookings` positionsbasiert (`SELECT *`) |
| DatevKey | 23 | INTEGER | | BU-Schlüssel, den die Quelle der Buchung mitgebracht hat (2.3.0, Schema 2003): ganze Zahl 1–9999, NULL = keiner (0 wird nie gespeichert) – der DATEV-Export gibt ihn unverändert aus, sonst leitet er ihn ab. Nur die Importe schreiben ihn – der WISO-Datenbank-Import an eine vorhandene Zeile nur, wenn sie denselben Steuersatz trägt wie der WISO-Satz (`datev_key_rate_mismatches`); er fällt (NULL), wenn sich Konto, Gegenkonto oder Steuersatz ändern (`update_booking_fields`, `update_booking`, der Tabellen-Export des Textimports, der ein leeres Konto füllt) |

**Business Logic:**
- `BookingType='bank'`: Echte Geldbewegung auf dem Bankkonto (aus WISO-Tabellen-Export oder PDF-Import)
- `BookingType='entry'`: Buchhalterischer Eintrag (aus WISO-Original-Export oder manuell)
- `ParentBooking_ID`: Verknüpft einen Entry mit seiner zugehörigen Bankbuchung
- Doppik-Entries (COA_ID **und** CounterCOA_ID sind eigene Bank-/Kassenkonten, `is_mirror_row`) werden
  in der Anzeige und im Buchungssatz-Editor ausgeblendet; ein Spiegel mit dem Verrechnungskonto 1460
  bleibt sichtbar und bearbeitbar
- `link_bank_to_entries()` verknüpft automatisch anhand mehrstufigem Matching (siehe Datenfluss)
- `Status='resolved'`: Debitoren-Entry wurde über Stufe 7 als erledigt markiert (Zahlung existiert)
- `Status='invoice_payment'`: Marke der Buchungssätze, die die App für eine Zahlungszuordnung angelegt hat (Abschnitt 9a). Sie braucht keine Schemaänderung – `Status` ist frei definierbar, die EÜR filtert nur auf `'resolved'`
- **Bankwirksamkeit** (`db.matching.is_bank_effective`): Unter einer Bankbewegung
  hängen auch reine Umbuchungen – die Zahlungs-Umbuchung `4405 → 4400` einer
  Rechnung oder ein Privatanteil `6805 → 2100`. Nicht bankwirksam ist ein Satz,
  wenn BEIDE Seiten kontiert sind und KEINE davon ein Bankkonto ist. Nur
  bankwirksame Sätze zählen in die Split-Summe (Statusbadge, DATEV-Prüfung);
  unvollständig kontierte zählen bewusst mit.
- **Form eines Buchungssatzes** (`Bookings.EntryForm`, `db/matching.py`): Sie steht beim
  Schreiben fest, statt bei jedem Lesen aus der Liste der Geldkonten von heute geraten zu
  werden. Wohin sich der Betrag bezieht und welche Seite bei positivem Betrag im Soll steht
  (`soll_side`):

  | Form | Geldseite | Betrag = Richtung auf … | bei Betrag > 0 im Soll |
  |------|-----------|-------------------------|------------------------|
  | `purpose_first` | Gegenkonto oder leer | dem Gegenkonto | Gegenkonto |
  | `liquid_first` | Konto oder leer | dem Konto | Konto |
  | `transfer` | keine (beide Seiten kontiert) | – | Konto |
  | `mirror_coa` | beide (Spiegel, Doppik) | dem Konto (Rohbetrag der Importe) | Konto |
  | `mirror_counter` | beide | dem Gegenkonto (so schreibt die Buchungsmaske) | Gegenkonto |
  | `mirror_legacy` | beide | unbekannt (Altbestand) | Gegenkonto |

  Bei Zahlungen trägt `Amount` die Richtung der Geldbewegung (Einnahme positiv), egal auf welcher
  Seite das Zweckkonto steht; bei Umbuchungen heißt positiv: `COA_ID` im Soll, `CounterCOA_ID` im
  Haben. Fehlt das Zweckkonto, wird `purpose_first` gespeichert (die EÜR bucht dann nichts).
  Jeder Schreibweg setzt die Form (Maske, Buchungssatz-Editor, Rechnung, AfA, Importe); ändern
  sich Konto oder Gegenkonto, wird sie neu abgeleitet – eine Seite, die gleich bleibt, behält ihre
  Rolle, und kippt die Seite des Betrags, wird der Betrag negiert (`reform`) – und mit ihm der
  Steuerbetrag, der das Vorzeichen des Betrags trägt, sofern der Aufrufer ihn nicht ändert
  (`_form_after_accounts_change`). EÜR, DATEV-Export,
  Dashboard und Split-Summe lesen die Form (`read_entry_form`); nur eine Zeile **ohne** Form
  (`NULL`, per SQL-Konsole angelegt) wird mit den Zahlungskonten von jetzt eingeordnet
  (`classify_entry`, Spiegel dann wie `mirror_legacy`).

  *Migration auf 2.1.0:* Jeder `entry` des Bestands bekam die Form, die 2.0.x ihm gab (die
  Zahlungskonten von damals einschließlich 1000–1099); Spiegel wurden `mirror_legacy`. Danach ist
  jede Auswertung identisch. Vor jedem Migrationslauf einer bestehenden Datenbank legt die App eine
  Sicherung nach `backup/JJJJMMTT_vor_<Programmversion>_<Datei>` an (erst als `.part`, dann
  umbenannt; nie überschrieben); die Umstellung selbst läuft in einer Transaktion. Kein Import und
  keine Maske stellt einen `mirror_legacy`-Spiegel um – die Importe zählen ihn nur (`legacy_mirrors`).

  *Masken:* Das Zweckkonto, das der Buchungssatz-Editor im Feld „SKR-Konto“ zeigt und
  „vorhandene Buchung zuordnen“ vorbelegt, bestimmt `purpose_in_counter`: `liquid_first` → das
  Gegenkonto, `purpose_first`/`transfer` → das Konto, Spiegel und Zeilen ohne Form wie in 2.0.x
  (Konto ist ein eigenes Bankkonto, Gegenkonto nicht → das Gegenkonto). Öffnen und unverändert
  speichern ändert keine Spalte. „Vorhandene Buchung zuordnen“ nimmt Steuersatz und -betrag der
  Buchung in die Zeile (`data-rate`/`data-tax` der Option) und schreibt sie nur, wenn das Formular sie
  mitbringt; Datum, Steuerdatum und Empfänger eines Satzes folgen der Bankbewegung nur, wenn er sie
  vorher mit ihr geteilt hat (`_plan_split_rows`, je Feld gleicher Wert wie die Bankbewegung bis jetzt).
  Steht auf der Geldseite eines Satzes (hinten, Zweckkonto vorne) schon ein anderes Zahlungskonto,
  überschreibt das Zuordnen es nicht; ein leeres Gegenkonto füllt das Konto der Bank.
  Entfernt der Nutzer die Zeile eines importierten Satzes (`SourceGroup`), einer AfA-Buchung oder einer
  als Kind angehängten Forderung, wird der Satz nur abgehängt (`remove_split_row`), jeder andere gelöscht.
- **Split-Summe** (`bank_effect`): Ein Spiegel mit bekannter Lesart (`mirror_coa`,
  `mirror_counter`) wirkt auf das Konto der Bankbewegung nach seiner Position – steht das Konto
  der Bank auf der Seite, auf die sich der Betrag bezieht, ist es der Betrag, auf der anderen
  Seite sein Gegenteil, berührt er das Konto nicht, 0. Alles andere zählt über
  `is_bank_effective`. Die Summenzeile des Editors rechnet genauso (`data-sign` je Zeile).
- **AfA-Buchung** (`book_depreciation`): eine Umbuchung „Aufwand an Anlagekonto“ als
  eigenständiger Satz – `COA_ID` = Aufwandskonto (z. B. 6220), `CounterCOA_ID` =
  Anlagekonto (z. B. 520), `Amount` positiv, `Account_ID` leer, `Status='posted'`,
  Datum 31.12. Sie bewegt kein Geld und ist über `AssetDepreciations.Booking_ID`
  verknüpft. Das Aufwandskonto muss ein Erfolgskonto (4000–7999), das Anlagekonto ein
  Anlagekonto (1–999) sein, sonst lehnt `book_depreciation` ab (`ValueError`); nach dem
  Abgang der Anlage (`Assets.SaleDate`) wird keine AfA mehr gebucht. Ältere Sätze
  (Aufwandskonto mit `Account_ID`, kein Gegenkonto) bleiben lesbar und zählen in der EÜR
  einmal; im DATEV-Export fehlt ihnen das Gegenkonto.

---

### 9a. Offene Posten (Rechnung → Zahlung)

Eine Rechnung wird mit dem Status `sent` als offener Posten gebucht und erst
bei Zahlung ertragswirksam: Erlös und Umsatzsteuer entstehen erst mit der
Zahlung (Ist-Versteuerung); bis dahin steht der offene Posten auf dem
Wartekonto, so sieht ihn auch das Steuerbüro im DATEV-Export. Das entspricht dem
Verfahren von WISO Mein Büro und damit auch der Struktur importierter Altdaten.
Die Besteuerungsart der eigenen Firma (`Contacts.VatMethod`, seit 2.4.0, siehe 3a)
ändert daran nichts: Sie wird nur gespeichert und angezeigt, Soll-Versteuerung
(Umsatzsteuer mit der Leistung) bildet die App noch nicht ab.

| Zeitpunkt | Buchung | Soll → Haben |
|---|---|---|
| Rechnung versendet | Forderung | `10000 Debitoren` → Wartekonto |
| Zahlung (Kind der Bankbewegung, `Status='invoice_payment'`) | `Zahlung zu Re. X` | Bankkonto → `10000 Debitoren` |
| Zahlung (Kind der Bankbewegung, `Status='invoice_payment'`) | `Umb. zu Re. X` | Wartekonto → Erlöskonto |

Wartekonten nach Steuersatz: **19 % → 4405**, **7 % → 4345**, **0 %/§19 → 4340**
(`Database.resolve_open_revenue_coa`). Erlöskonten wie bisher 4400/4300/4185
(`resolve_revenue_coa`). Sätze ohne Wartekonto (z. B. 5 %) werden weiterhin erst
bei Zahlung gebucht.

Die Forderungsbuchung ist ein eigenständiger Entry ohne `Account_ID` und ohne
`ParentBooking_ID`; erkannt wird sie über `DocumentNumber` + Debitorenkonto
(`get_receivable_booking`). `ensure_receivable_booking` ist idempotent,
`remove_receivable_booking` nimmt sie beim Rückstufen auf Entwurf zurück –
aber nur, solange keine Zahlung daran hängt, und nur die Forderung, die die App selbst gebucht
hat (ohne `SourceGroup`, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet:
`_receivable_removal`); eine importierte oder von Hand gebuchte bleibt immer stehen.
Der Statuswechsel einer Rechnung (`Database.set_invoice_status`) bucht und entfernt die
Forderung in derselben Transaktion wie den Status: Beim Versenden und Reaktivieren (nur eine
stornierte Rechnung ohne Stornorechnung) wird sie gebucht, beim Zurückholen auf Entwurf (nur ohne
Zahlung) entfernt. Auf „storniert“ setzt der Status nur eine Rechnung ohne positiven Betrag (Gutschrift,
0-€-Rechnung – sie bekommt keine Stornorechnung): Die Forderung der App geht mit, solange keine Zahlung daran hängt.
Für jede andere ist das `cancel_invoice` (Abschnitt 9b) – es löscht die Forderung nie, sondern bucht ihre
Gegenbuchung. Bei einem Storno-Paar (Rechnung mit
Stornorechnung und die Stornorechnung selbst) entfernen weder `remove_receivable_booking` noch das
Entfernen der letzten Zahlung etwas (`_drop_receivable`).
Hängt an einer Rechnung eine Forderung, die die App nicht entfernen darf, lässt sie sich nicht
auf Entwurf zurückholen (Klartext, nichts geändert). Beim Zurückholen wandern PDF und XML in
`invoices/<Jahr>/Fassungen/<ID>/`; der Ordner gilt als „war schon versendet“, die Rechnungsnummer
wird dann nie wieder vergeben. Jeder gelungene Wechsel steht im Protokoll `InvoiceCorrections`
(Abschnitt 14b), und beim ersten Versand setzt `send` den Zeitpunkt `Invoices.SentAt`.

**Auswertungsneutral:** Forderungen (10000) und Wartekonten (4340/4345/4405)
sind Bestandskonten und bleiben aus der EÜR heraus
(`db.reporting.NEUTRAL_ACCOUNT_NUMBERS`). Einnahmezeitpunkt ist die Umbuchung
auf das Erlöskonto. Ohne diese Regel höbe `4405 −netto` den Erlös `4400 +netto`
wieder auf (beide liegen in der Erlösklasse 4000–4999) und die Forderung
erschiene als Ausgabe.

**Zahlungszuordnung (Rechnung ↔ Bankbewegung):** Eine Zuordnung besteht aus genau einer
`InvoicePayments`-Zeile und den Sätzen, die die App für sie angelegt hat. Diese Sätze sind eindeutig
bestimmt durch `ParentBooking_ID` (die Bankbewegung), `DocumentNumber` (die Rechnungsnummer) und die
Marke `Status='invoice_payment'`. Es gibt eine Stelle, die Zahlungen zuordnet
(`assign_invoice_payment`, `db/invoice_payments.py`), eine, die eine Stornorechnung erstattet
(`assign_storno_refund`, Abschnitt 9b), und eine, die Zuordnungen entfernt (`unassign_invoice_payment`).

- **Voraussetzungen:** nur Zahlungseingänge (Bankbewegung mit Betrag > 0) auf versendete Rechnungen
  (`sent`, `overdue`, `partial_payment`; `PAYABLE_STATUSES`) mit positivem Brutto; das Bankkonto der
  Bewegung braucht eine SKR-Nummer; das Paar (Rechnung, Bewegung) gibt es höchstens einmal. Negative
  Zuordnungen (Storno, Gutschrift) kennt der WISO-Import (`link_invoice_to_transaction`, mit
  Vorzeichen und ohne eigene Sätze) und die Erstattung einer Stornorechnung (`assign_storno_refund`, mit Sätzen).
  Einer **Stornorechnung** (`InvoiceKind='storno'`) lässt sich nie eine
  Zahlung zuordnen (`payment_blocker`; auch `link_invoice_to_transaction` lehnt ab), und sie ist nie ein
  Kandidat für die Zuordnung – ihre Zuordnung ist allein die Erstattung.
- **Betrag:** min(Rest der Rechnung, freier Rest der Bewegung, gewünschter Betrag); der gewünschte Betrag
  wird vorher auf Cent gerundet (halb auf, `118,995 → 119,00`; ≤ 0 nach dem Runden lehnt ab), sonst bliebe
  ein Bruchteil eines Cents auf dem Debitorenkonto stehen. Frei ist
  `Betrag − MAX(Σ Zuordnungen, Σ bankwirksame Sätze darunter)` (`_free_minor_sql`, dieselbe Formel für
  Zuordnung, Kandidatenlisten und Kontakt-Guthaben): eine schon als Erlös kontierte Bewegung ist keine
  Rechnungszahlung mehr. Für den Ausgang einer Erstattung gilt
  `|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze auf das Konto der Bewegung)` (`_outgoing_free_minor`, in
  Python: die Wirkung rechnet `db.matching.bank_effect` – wie die Split-Summe, ein Spiegel mit bekannter Lesart zählt nach
  seiner Position auf dem Konto der Bewegung; der Import-Spiegel „1810 an 1800“ mit +50 unter einem Ausgang über 50
  verbraucht ihn, die Umbuchung ist keine Erstattung). Anders als `_free_minor_sql` für Eingänge, das einen Spiegel roh
  rechnet (bewusst unverändert). Die Kandidatenliste der Erstattung liest den freien Rest aller Ausgänge aus wenigen gesammelten
  Abfragen (Aufwand linear). *Sammelzahlung* = mehrere Zuordnungen derselben Bewegung, jede mit eigenen
  Sätzen; *Teilzahlung* = mehrere Bewegungen auf eine Rechnung; bei einer *Überzahlung* bleibt der Rest
  frei (Guthaben beim Kontakt; der DATEV-Export bleibt gesperrt, bis er kontiert ist).
- **Erlöskonto:** Es ergibt sich aus der Rechnung (`resolve_revenue_coa`: gelernt, sonst 4400 bei
  19 %, 4300 bei 7 %, 4185 steuerfrei/§ 19). **Gelernt wird nur aus den markierten Sätzen mit positivem
  Betrag** der Zuordnungen (`Status='invoice_payment'`; die Sätze einer Erstattung lehren nichts) von Rechnungen
  mit demselben Steuersatz, und nur in der Form,
  in der die App sie schreibt: bei der Umbuchung „Wartekonto an Erlöskonto“ (`COA` ist ein Wartekonto)
  das Gegenkonto, beim Einzelsatz „Erlöskonto an Bank“ (das Gegenkonto ist ein Zahlungskonto) das Konto;
  der jüngste Satz gewinnt. Ein Satz, den ein Nutzer im Split-Editor in eine andere Form gebracht hat
  (aus „4405 an 4400“ wurde „4736 an 4400“), lehrt nichts. Das Ergebnis gilt nur, wenn es ein
  Erlöskonto 4000–4999 (`REVENUE_ACCOUNT_RANGE`) und kein Wartekonto (4340/4345/4405) ist, sonst
  greift das Standardkonto. Das Konto der Bankzeile (der Anzeige-Backfill) und die Sätze des
  WISO-Imports lehren nichts: Dort kann ein Bank-, Aufwands- oder Wartekonto stehen. Nur wenn für den Satz kein Konto hinterlegt ist – z. B. bei 5 % oder 16 % ohne
  Historie – wählt der Nutzer es (`revenue_coa_id`, im Formular „Erlöskonto“): ein vorhandenes
  Erlöskonto (`get_revenue_coas`, kein Wartekonto), sonst lehnt `assign_invoice_payment` ab – die
  Prüfung liegt in der DB-Schicht. Ist ein Konto hinterlegt, wird die Wahl ignoriert.
  `suggest_revenue_coa(Steuersatz)` liefert nur die Vorauswahl (das häufigste Erlöskonto unter den
  Buchungen mit diesem Satz, bei Gleichstand die kleinste Nummer); eine Entscheidung trifft der Nutzer.
  Die Wahl des Nutzers merkt sich die Zuordnung in ihren Sätzen: Die nächste Rechnung mit diesem Satz
  braucht keine Wahl mehr. Für Sätze ohne Wartekonto entsteht kein Wartekonto – es bleibt bei dem einen
  Satz `Erlöskonto → Bank`.
- **Sätze:** mit Forderung `Bank → 10000` (ohne Steuer) und `Wartekonto → Erlöskonto` (mit Steuer);
  ohne Forderung (Satz ohne Wartekonto, Bestand vor den Forderungsbuchungen) ein Satz
  `Erlöskonto → Bank` über den zugeordneten Betrag. Datum, Steuerdatum und Empfänger stammen von der
  Bewegung, der Kontakt von der Rechnung. Die Steuer eines Teils ist der anteilige Rechnungsbetrag
  (auf den Cent gerundet); der Teil, der die Rechnung schließt, bekommt den Rest: Rechnungssteuer minus
  die Steuer der anderen Zuordnungen. Hat eine markierte Sätze, zählt deren tatsächlich gebuchte Steuer;
  eine Zuordnung ohne markierte Sätze (Altbestand, WISO-Import) zählt mit ihrem anteiligen Betrag – die
  Summe der Zahlungssätze stimmt so mit der Steuer der Rechnung überein, auch wenn die Sätze einer
  Altzahlung eine andere oder keine Beleg-Nr. tragen.
- **Gleichzeitigkeit:** Prüfung, Schreiben und Neuberechnung laufen in einer `BEGIN IMMEDIATE`-Transaktion;
  zwei gleichzeitige Anfragen legen das Paar nicht doppelt an (zusätzlich fängt der UNIQUE-Index
  `ux_invoicepayments_invoice_booking` es ab, Abschnitt 14). Konten,
  die dabei angelegt werden könnten (`resolve_revenue_coa`), löst die Funktion vorher in eigenen
  Verbindungen auf.
- **Entfernen:** `unassign_invoice_payment` löscht die markierten Sätze des Paars (nie Sätze mit
  `SourceGroup`, nie Sätze ohne Marke – Altbestand und WISO-Sätze bleiben stehen) und die Zuordnung,
  setzt eine durch Stufe 7 aufgelöste Forderung wieder auf offen, wenn die übrigen Sätze „… an Debitoren“
  sie nicht mehr ausgleichen (Summe < Forderungsbetrag – bei mehreren Forderungszeilen der Beleg-Nr. deren Summe –, `money.is_covered`), und rechnet die Rechnung
  neu. War es die letzte Zahlung und ist der Status der Rechnung kein Forderungsstatus
  (`RECEIVABLE_STATUSES`: versendet, Teilzahlung, überfällig, bezahlt – also etwa storniert), geht auch
  die Forderung (die die App selbst gebucht hat, nie ein Satz mit `SourceGroup`), wie beim Statuswechsel:
  „erst von Hand stornieren, dann die Zahlung entfernen“ endet im selben Zustand wie die umgekehrte Reihenfolge –
  nicht aber bei einer Rechnung mit Stornorechnung: Ihre Forderung und die Gegenbuchung bleiben stehen. `delete_transaction` einer Bankbewegung nimmt ihre Sätze und
  Zuordnungen mit; das Löschen eines markierten Satzes (auch im Split-Editor) entfernt die ganze
  Zuordnung. Eine Erstattung geht auf demselben Weg; eine Zahlung an einer stornierten Rechnung mit Stornorechnung
  lässt sich dagegen auf **keinem** Weg entfernen, wenn danach mehr erstattet als gezahlt wäre (`PaymentRemovalRefused`,
  `_refund_removal_blocker`, vor dem ersten Schreiben in der Transaktion): nicht auf der Rechnungsseite, nicht mit dem ✕
  an einer Zeile der Zahlung im Bank-Editor (der Editor prüft alle entfernten Zeilen zusammen beim Planen; seit 2.3.15 läuft
  sein Speichern in einer Transaktion – `save_bank_with_rows` –, bei einer Ablehnung oder einem Fehler ist nichts gespeichert), nicht mit dem Löschen eines Satzes oder der Bankbewegung (`delete_transaction`, Liste:
  Weiterleitung mit dem Grund). Das Entfernen einer Erstattung bleibt frei.
- **Status der Rechnung** (`next_payment_status`, vorzeichenbewusst für die WISO-Gutschrift): Zahlung in
  Höhe der Rechnung (Rest unter einem Cent) → `paid`, sonst `partial_payment`; ohne Zahlung wieder
  `overdue` (Fälligkeit überschritten) bzw. `sent` – nie zurück auf `finalized`, eine stornierte
  Rechnung bleibt `cancelled`, und der Status einer Stornorechnung ändert sich nie. Zahlungen lassen sich
  erst nach dem Versenden zuordnen.
- **Die Forderung ist nie Kind einer Bankbewegung.** Der Auto-Abgleich (`link_bank_to_entries`,
  alle Stufen außer der Quellklammer 0) und die Kandidaten des Buchungssatz-Editors schließen sie aus;
  `delete_transaction` hängt eine als Kind vorgefundene Forderung ab, statt sie zu löschen. Die Regel
  steht an einer Stelle (`db.matching.receivable_condition`): Der Satz führt Debitoren (10000) im **Soll**
  – nach der Vorzeichenregel der Umbuchungen `COA` = 10000 bei Betrag > 0 oder Gegenkonto = 10000 bei
  Betrag < 0; bei einer **Gutschrift** (Rechnung mit negativem Brutto) bucht die App „10000 an Wartekonto“
  mit negativem Betrag, dann gilt auch `COA` = 10000 mit Betrag < 0, sofern die Beleg-Nr. die Nummer einer
  Rechnung mit negativem Brutto ist – und hat auf keiner Seite ein Zahlungskonto. Was 10000 im Haben
  führt (die Zahlung „Bank an 10000“, Erlösminderung, Skonto, Forderungsverlust „… an 10000“), ist
  keine Forderung; solche Sätze gehören zur Bewegung und werden mit ihr gelöscht. Auch `assign_invoice_payment` und
  `get_receivable_booking` suchen die Forderung so (nicht „erster Satz mit Konto 10000“, der einen
  WISO-Storno „10000 an 1810“ träfe). Stufe 7 des Auto-Abgleichs setzt die Forderung erst auf
  `resolved`, wenn die Summe der Sätze „… an 10000“ mit gleicher Beleg-Nr. den Forderungsbetrag erreicht
  (`money.is_covered`) – nicht schon nach der ersten Teilzahlung. Trägt die Beleg-Nr. mehrere eigenständige
  Forderungszeilen, zählt die Summe aller; sie werden gemeinsam aufgelöst und gemeinsam zurückgesetzt (seit 2.6.8).
- **Prüfung:** `find_payment_inconsistencies(invoice_id=None)` meldet, rein lesend, Zuordnungen ohne Satz
  „Bank an Debitoren“, Sätze „Bank an Debitoren“ ohne Forderung (10000 im Haben-Überhang), an einer
  Bankbewegung hängende Forderungen, Zahlungssätze ohne Zuordnung (die markierten Sätze der App und
  bankwirksame Erlössätze unter einer Bewegung, deren Beleg-Nr. eine Rechnungsnummer ist, ohne Zuordnung
  für das Paar – unabhängig vom Text), Zuordnung ≠ Umbuchung, Zuordnungen auf einen Satz oder einen
  Ausgang, doppelte Zuordnungen, versendete Rechnungen mit Status „Entwurf/abgeschlossen“ und einen Rest,
  der nicht zu den Zuordnungen passt (der Rest einer Stornorechnung ist ihr negativer Betrag: kein Befund). Zwei
  Kategorien gelten dem Storno-Paar und zählen nur Forderungen, die die App selbst gebucht hat (ohne `SourceGroup`,
  Wartekonto als Gegenkonto): `storno_counter_mismatch` – von der Forderung der Rechnung und der Gegenbuchung der
  Stornorechnung steht nur eine – und `legacy_cancelled_open` – eine von Hand stornierte Rechnung (Brutto > 0) ohne
  Stornorechnung, deren Forderung noch in den Büchern steht (Abhilfe: „Stornorechnung nachträglich erstellen“). Dazu
  `refund_exceeds_credit`: Zur Stornorechnung ist mehr erstattet als zur stornierten Rechnung gezahlt wurde (Σ Zuordnungen
  beider Belege < 0) – die App lässt es nicht mehr entstehen, die Kategorie meldet Eingriffe an ihr vorbei. Ein intaktes Paar, auch mit Guthaben aus Zahlungen auf die stornierte Rechnung oder mit Erstattung,
  ergibt keinen Befund. Zuordnungen, deren Buchung keine Bankbewegung ist oder deren Sätze
  eine `SourceGroup` tragen (WISO liefert seine Sätze selbst, z. B. Barzahlungen ohne Bankbewegung),
  lösen weder „Zuordnung ohne Ausgleichssatz“ noch „Zuordnung ≠ Umbuchung“ aus. Die Abfragen gehen von
  den Zuordnungen aus bzw. fassen je Rechnung vorab zusammen: Der Aufwand wächst linear mit dem Bestand
  (4000 Bewegungen, 4000 Forderungen, 1000 Zuordnungen: unter einer Sekunde). Die Forderungen der App für die beiden
  Storno-Kategorien liest die Liste einmal für alle Paare und Alt-Stornos (`_app_receivable_amounts`), nicht je
  Rechnung nachgeschlagen – die Zahl der Anweisungen hängt nicht von der Zahl der Paare ab. Die Liste steht auf der
  Seite *Sonstiges* und läuft **nur auf Klick** („Zahlungszuordnungen prüfen“, `GET
  /miscellaneous?check_payments=1`), nicht bei jedem Laden der Seite; der Hinweis auf der Rechnungsseite
  prüft nur diese eine Rechnung.
- **Neu aufbauen (seit 2.5.13, `rebuild_invoice_payments`, `db/invoice_repair.py`):** Auf der Seite einer Rechnung lässt sich ihre
  Zuordnung nach einem Probelauf neu aufbauen – je Rechnung, nie für den ganzen Bestand. Sie baut nur auf, was die Prüfliste meldet,
  und **löscht und ändert keinen Buchungssatz**: Doppelte Zuordnungen desselben Paares mit gleichem Betrag werden
  zusammengefasst (die kleinste ID bleibt), die als Kind an einer Bankbewegung hängende Forderung der App wird gelöst
  (`ParentBooking_ID` und Status), eine Zuordnung ohne eigene Sätze bekommt „Bank an Debitoren“ und „Wartekonto an Erlöskonto“
  (mit Datum der Bankbewegung, Marke und der Steuer wie bei jeder Zuordnung), und der offene Betrag wird neu gerechnet; der Status
  bleibt, außer nach dem Zusammenfassen von Doppelten. Neu aufgebaut wird nur, wenn alle Bedingungen zutreffen: Zahlungseingang auf einer
  Bankbewegung mit SKR-Nummer, keine Sätze mit Quellklammer und keine mit der Nummer dieser Rechnung unter der Bewegung, alle übrigen Sätze
  gehören einer anderen zugeordneten Rechnung, genau eine Forderung der App, ein Betrag in ganzen Cent, ein hinterlegtes oder gewähltes
  Erlöskonto (angelegt wird nie eines). Dazu darf die Rechnung nirgends sonst Buchungssätze haben: Trägt ein Satz außerhalb ihrer
  zugeordneten Bewegungen ihre Nummer (eigenständig, auf der Kasse, unter einer fremden Bewegung, mit oder ohne Quellklammer), steht ihr Erlös
  schon in den Büchern, und es wird nichts aufgebaut, damit er nicht zweimal gebucht wird – ebenso, wenn die Sätze unter den zugeordneten
  Bewegungen mehr Erlös buchen, als die übrigen Zuordnungen erklären (je Rechnung, und je Bewegung gegen die Zuordnungen der anderen
  Rechnungen dort); einen Satz ohne ihre Nummer erkennt die Funktion nicht, deshalb zeigt der Probelauf die Wirkung auf die EÜR.
  Mehrere Zuordnungen einer Rechnung werden in zeitlicher Folge neu gebucht –
  die Steuer verteilt sich wie beim Zuordnen nacheinander, die letzte Zuordnung bekommt den Rest. Storno-Paare, Rechnungen außerhalb von
  versendet bis bezahlt und alles, was nicht eindeutig ist, bleiben ein Hinweis mit Grund. Der Probelauf ist derselbe Code wie die
  Ausführung und endet mit Rollback (kein Zähler, kein
  Replay-Log); er zeigt die neuen Sätze, die Wirkung auf die EÜR je Datum und Erlöskonto und den Rest der Prüfliste. Ausgeführt wird nur mit
  der Prüfsumme des Probelaufs – hat sich der Bestand geändert, kommt der neue Plan und nichts ist geschrieben. Die Wirkung auf die EÜR
  entsteht am Datum der Bankbewegung: Ist der Zeitraum schon gemeldet, ist die Änderung dort nachzutragen.

---

### 9b. Stornorechnung (`cancel_invoice`, seit 2.2.1)

Eine versendete Rechnung wird nicht über den Status storniert, sondern über „Stornieren“
(`Database.cancel_invoice`, `db/invoice_storno.py`; Route `POST /invoice/cancel`). Ausnahme: Eine Rechnung ohne
positiven Betrag (Brutto ≤ 0) bekommt keine Stornorechnung – sie lässt sich über den Status auf „storniert“ setzen
(Abschnitt 9a). Das Stornodatum liegt nicht vor dem Rechnungsdatum und nicht in der Zukunft (nach dem Tag des Servers;
ein Tippfehler legte sonst einen Nummernkreis 2062 an). Es entsteht ein eigener Beleg in `Invoices` (Abschnitt 12):

- **Stornorechnung:** Nummer aus dem Rechnungskreis des Jahres des Stornodatums; Kopf der
  Ursprungsrechnung (Anschrift-Momentaufnahme, Leistungsdatum, Bank, Steuer, Leitweg-ID) ohne
  Fälligkeit und Skonto, `PaymentTerms` = „Der Betrag wird verrechnet bzw. erstattet.“,
  `InvoiceKind='storno'`, `CancelsInvoiceId` = die Ursprungsrechnung, Status `sent` mit `SentAt`;
  `SumNet`, `TaxAmount`, `SumGross` **exakt negiert** (Minor Units mal −1, nichts neu gerechnet),
  `AmountDue` = ihr negatives Brutto. Positionen: dieselbe Reihenfolge, `Quantity` und `TotalNet`
  negiert, `PricePerUnit` unverändert. Die Ursprungsrechnung wird `cancelled`; beide Belege bekommen eine
  Protokollzeile (`Action='status'`).
- **Gegenbuchung:** Hat die App die Forderung gebucht (ohne `SourceGroup`, Wartekonto als Gegenkonto – gleich,
  ob sie an einer Bankbewegung hängt), entsteht „Debitoren an Wartekonto“ mit **negativem** Betrag und
  negativer Steuer, Datum = Stornodatum, Beleg-Nr. = Stornonummer, Form `transfer`. Für
  `db.matching.receivable_condition` ist das die Forderung einer Gutschrift (Beleg-Nr. einer Rechnung mit
  negativem Brutto). Die Forderung der Ursprungsrechnung bleibt stehen; Debitoren und Wartekonto sind danach
  ausgeglichen, die EÜR ändert sich nicht. Ohne Forderung der App (Import, Steuersatz ohne Wartekonto,
  importierte oder von Hand gebuchte Forderung) gibt es keine Gegenbuchung.
- **Zahlungen** bleiben an der stornierten Rechnung (Zahlungen und ihre Sätze unverändert); wurde schon
  gezahlt, steht das Guthaben des Kunden als Haben-Saldo auf den Debitoren, bis es auf die Ersatzrechnung
  übergeht (siehe unten) oder erstattet wird (siehe *Erstattung*). Der Stornorechnung lässt sich keine
  Zahlung zuordnen, nur eine Erstattung.
- **Guthaben** = Summe von `InvoicePayments.Amount` über beide Belege des Paares (stornierte Rechnung und
  Stornorechnung; `get_storno_credits`, `get_storno_credit`, `get_storno_credits_for_contact`), rein lesend: in der
  Rechnungsliste „Erstattung offen: …“ an der Stornorechnung (nach voller Erstattung wieder „Stornorechnung“), als Zeile
  auf beiden Seiten, bei den offenen Posten des Kontakts (`storno_credits`; der Saldo zieht es ab, weil 10000 es im Haben
  führt). Alle zeigen das verbleibende Guthaben.
- **Erstattung** (`Database.assign_storno_refund`, seit 2.2.3): Eine ausgehende Bankbewegung (Betrag < 0, Bankkonto mit
  SKR-Nummer) wird der Stornorechnung zugeordnet – eine `InvoicePayments`-Zeile mit **negativem** Betrag und Sätze mit der
  **Nummer der Stornorechnung** als Beleg-Nr. (Marke `invoice_payment`, Datum, Steuerdatum, Empfänger und Währung der
  Bewegung), negativ: Hat die Stornorechnung eine Gegenbuchung, `Bank → 10000` und `Wartekonto → Erlöskonto` (mit Steuer),
  sonst der Einzelsatz `Erlöskonto → Bank`. Erstattet wird min(Guthaben, freier Rest der Bewegung, gewünschter Betrag); eine
  Überweisung kann mehrere Stornorechnungen erstatten. Die Steuer nimmt zurück, was die Zahlungen an der stornierten Rechnung
  gebucht haben: anteilig, die Erstattung, die das Guthaben ausgleicht, den Rest – die Summe aller Erstattungen ist die Steuer
  der Zahlungen, solange sich die Zahlungen der stornierten Rechnung nach der ersten Erstattung nicht ändern (wird danach eine
  Zahlung entfernt oder zieht ein Teil um, kann die Summe um Cent abweichen). Das Erlöskonto ist das der Zahlungen, wenn alle
  dasselbe tragen; liegen sie auf mehr als einem Erlöskonto (ein Satz wurde im Editor geändert), wählt der Nutzer das der
  Erstattung (die Seite zeigt die Auswahl mit dem Hinweis); ohne markierte Sätze (Import) ist es das hinterlegte oder die Wahl
  des Nutzers. In der EÜR stehen negativer Erlös und negative Umsatzsteuer am Tag der Erstattung; das Dashboard zählt die
  Bankbewegung als Ausgabe. Nach einer Erstattung zieht höchstens das verbleibende Guthaben auf die Ersatzrechnung um (siehe
  *Zahlungen ziehen um*).
- **Gesperrt:** Die Stornorechnung ist ganz gesperrt (`db.invoice_lock.row_group`): Status, Speichern, Berichtigen,
  Löschen, Kopieren werden abgelehnt. Eine stornierte Rechnung mit Stornorechnung lässt sich nicht
  wiederbeleben.
- **Dokument:** CII Typ 381 mit `ram:InvoiceReferencedDocument` (BT-25/BT-26), UBL als `CreditNote` mit
  `cac:BillingReference`; im XML stehen die Beträge positiv, das PDF zeigt die gespeicherten negativen.

Alles läuft in einer Transaktion (`BEGIN IMMEDIATE`): Es gelingt ganz oder gar nicht, eine Nummer wird nur bei
Erfolg verbraucht. Zeigt dabei ein Verweis der Rechnung ins Leere (gelöschtes Bankkonto, Kunde …), nennt die Antwort ihn
im Klartext (409); repariert wird nichts.

**Nachträglich:** Eine von Hand stornierte Rechnung (Status `cancelled`, Brutto > 0) ohne Stornorechnung bekommt sie über
denselben Weg: Status und Protokoll der Rechnung bleiben, die Gegenbuchung folgt nur, wenn die App ihre Forderung gebucht
hat und sie noch steht.

**Neu ausstellen** (`Database.create_replacement_draft`, Route `POST /invoice/reissue` und `reissue` in
`/invoice/cancel`, seit 2.2.2): Zu einer stornierten Rechnung mit Stornorechnung entsteht der Entwurf einer neuen
Rechnung (Kopf und Positionen der stornierten, positiv; neue Nummer aus dem Kreis des heutigen Jahres, heutiges Datum,
Fälligkeit um das alte Zahlungsziel verschoben, `Status='draft'`) mit `ReplacesInvoiceId` = die stornierte Rechnung –
höchstens eine je stornierter Rechnung (geprüft in der Transaktion, ohne Index). Die Ersatzrechnung trägt in ihrem
Dokument den Hinweis „Ersetzt die stornierte Rechnung … (Stornorechnung …)“ (PDF, CII BT-22, UBL `cbc:Note`).

**Zahlungen ziehen um:** Beim Versenden der Ersatzrechnung (`set_invoice_status`, Art `send`) gehen die Zahlungen der
stornierten Rechnung in derselben Transaktion auf sie über (`_move_replaced_payments`): Je Zuordnung wird sie samt Sätzen
entfernt, die neue Rechnung bekommt, was sie noch braucht (eigene Sätze mit dem Datum der Bankbewegung, ihrem
Steuersatz und Erlöskonto; die Steuer eines Teils anteilig, der schließende Teil bekommt den Rest), ein Rest wird an
der stornierten Rechnung wieder zugeordnet. Umgezogen wird nur, was die App genau so geschrieben hat: Die Sätze einer
Zuordnung müssen die sein, die die Zuordnung dafür schreibt (Bestand, Bankkonto und Konten in ihrer Rolle, Betrag,
Datum, Steuerdatum, Steuersatz, Steuerbetrag – anteilig oder als schließender Teil der Rest –, keine Kategorie, kein Beleg
und kein anderer Verweis an den Sätzen, kein verwaister markierter Satz unter der Bewegung). Was sich so nicht eindeutig neu
schreiben lässt (Import, überbuchte Bewegung, geänderte oder ergänzte Sätze), bleibt an der stornierten Rechnung; eine
Sammelzahlung zieht nur mit dem Anteil der stornierten Rechnung um. Wurde zur stornierten Rechnung schon erstattet, zieht
höchstens das verbleibende Guthaben um (die Zahlungen minus die Erstattungen vor dem Umzug): Je Zuordnung ist der Teil
min(Betrag, Rest der neuen Rechnung, verbleibendes Guthaben), der Rest der Zuordnung bleibt an der stornierten Rechnung, und
nach dem Umzug ist nie mehr erstattet als gezahlt (50,00 € gezahlt, 20,00 € erstattet, Ersatzrechnung über 119,00 €: 30,00 €
ziehen um, 20,00 € bleiben); der Hinweis nennt das Erstattete. Gleicher Kunde und gleiche Währung sind
Voraussetzung, ebenso ein hinterlegtes Erlöskonto für den Steuersatz der neuen Rechnung. Ein Hinweis in der Antwort nennt,
was geschah und bei dem, was bleibt, den Grund (je Grund einmal). Jede Zuordnung läuft in einem Savepoint: Lässt sie sich
nicht neu buchen, bleibt sie unverändert liegen, und der Versand gelingt; nur ein Datenbankfehler mitten im Umzug lehnt
ihn ab (nichts geändert).

---

### 10. BookingDocuments (n:m)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | Auto-Increment |
| Booking_ID | INTEGER | NOT NULL, FK → Bookings | |
| Document_ID | INTEGER | NOT NULL, FK → Documents | |
| RelationType | TEXT | | Art der Beziehung: `receipt` \| `invoice` \| `contract` \| `other` (Positivliste, Standard `receipt`) |

**Indizes:** UNIQUE(Booking_ID, Document_ID)

Verknüpfbar ist jede Zeile von `Bookings` (Bankbewegung, Satz einer Bankbewegung, eigenständiger Satz). Die App verknüpft über
`Database.link_receipt_to_booking`: Trägt die Buchung keine Beleg-Nr. (`Bookings.DocumentNumber`) und ist die Verknüpfung von der Art
`receipt`, übernimmt sie in derselben Transaktion die Nummer des Belegs – nur die verknüpfte Zeile, nie ein Zahlungssatz einer
Rechnungszuordnung (`Status = 'invoice_payment'`) und nie eine Nummer, die eine Rechnung trägt (`Invoices.InvoiceNumber`). Ebenso nie
eine Nummer, die eine andere Buchung trägt oder der sie ähnelt (die eine in der anderen enthalten; ausgenommen die eigene Familie: die
Sätze der Buchung, ihre Bankbewegung und deren übrige Sätze), weil sie wie eine von Hand eingetragene Nummer auch auf den automatischen
Abgleich wirkt (dieselbe Nummer am selben Tag gilt dort als ein Beleg). Lösen entfernt nur die Verknüpfung; die Nummer bleibt an der Buchung.

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
| NumberFormat | TEXT | DEFAULT '{yy}{l}{nnn}{s}' | Format-Template, z.B. `{yy}{l}{nnn}{s}` → `26F001` |

**Indizes:** UNIQUE(Type, Year, Letter, Prefix)

---

### 12. Invoices (Ausgangsrechnungen)

**Zweck:** Rechnungsverwaltung für XRechnung/ZUGFeRD mit Snapshot-Prinzip

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
| SellerVATID | 10 | TEXT | | USt-IdNr. oder Steuernummer der eigenen Firma (eine Spalte für beides; `tax_id_kind` trennt sie) |
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
| BuyerVATID | 20 | TEXT | | USt-IdNr. des Käufers (eine Steuernummer fällt in der E-Rechnung weg: EN 16931 hat dafür kein Feld) |
| BuyerReference | 21 | TEXT | | Kundenreferenz |
| BuyerRouteID | 22 | TEXT | | Leitweg-ID (XRechnung) |
| **Auftrag / XRechnung** | | | | |
| OrderNumber | 23 | TEXT | | |
| Currency | 24 | TEXT | DEFAULT 'EUR' | |
| DeliveryDate | 25 | DATE | | |
| **Zahlungsbedingungen** | | | | |
| PaymentTerms | 26 | TEXT | | |
| PaymentDueDate | 27 | DATE | | |
| SkontoDays | 28 | INTEGER | | Skontofrist in Tagen ab Rechnungsdatum, ganze Zahl von 1 bis 365 (mit `SkontoPercent`; beide leer oder 0 = kein Skonto) |
| SkontoPercent | 29 | REAL | | Skonto in Prozent, größer 0 und kleiner 100, höchstens zwei Nachkommastellen |
| **Bankverbindung (Snapshot)** | | | | |
| BankAccountId | 30 | INTEGER | FK → Accounts | |
| BankName | 31 | TEXT | | |
| BankIBAN | 32 | TEXT | | |
| BankBIC | 33 | TEXT | | |
| **Summen** | | | | |
| TaxCategory | 34 | TEXT | DEFAULT 'S' | |
| TaxRate | 35 | REAL | NOT NULL | 0.19 = 19% |
| SumNet | 36 | INTEGER | NOT NULL | Minor Units |
| TaxAmount | 37 | INTEGER | NOT NULL | Minor Units |
| SumGross | 38 | INTEGER | NOT NULL | Minor Units |
| AmountDue | 39 | INTEGER | NOT NULL | Minor Units; offener Betrag |
| **Verwaltung** | | | | |
| Status | 40 | TEXT | DEFAULT 'draft' | draft / finalized / sent / overdue / partial_payment / paid / cancelled |
| PDFPath | 41 | TEXT | | Ablage `<Datenordner>/invoices/<Jahr>/<RNr.> <Kunde>.pdf` |
| XMLPath | 42 | TEXT | | XRechnung, gleicher Name mit `.xml` |
| CreatedAt | 43 | DATETIME | DEFAULT NOW | |
| UpdatedAt | 44 | DATETIME | | |
| **Angebot / Beleg** | | | | |
| DocumentType | 45 | TEXT | NOT NULL, DEFAULT 'invoice' | `'invoice'` \| `'quote'` |
| ValidUntil | 46 | DATE | | Angebot: gültig bis |
| IntroText | 47 | TEXT | | Angebot: Text vor den Positionen |
| ClosingText | 48 | TEXT | | Angebot: Text nach den Positionen |
| SourceQuoteId | 49 | INTEGER | FK → Invoices ON DELETE SET NULL | Rechnung: Quell-Angebot |
| **Fassungen, Versand, Stornobeleg (Schema 2002, seit 2.2.0)** | | | | |
| InvoiceKind | 50 | TEXT | | NULL = Rechnung, `'storno'` = Stornorechnung (seit 2.2.1) |
| CancelsInvoiceId | 51 | INTEGER | UNIQUE (Index `ux_invoices_cancels`, nur wenn gesetzt) | Stornorechnung: die stornierte Rechnung (seit 2.2.1) |
| ReplacesInvoiceId | 52 | INTEGER | | Ersatzrechnung: die ersetzte, stornierte Rechnung (seit 2.2.2, ohne Index; höchstens eine Ersatzrechnung je stornierter Rechnung, in der Transaktion geprüft) |
| SentAt | 53 | DATETIME | | erstmals versendet (UTC, gesetzt beim Übergang `send`); Import und Altbestand: NULL |
| DocumentVersion | 54 | INTEGER | | Nummer der aktuellen Fassung; NULL = Fassung 1 |

Die Spalten 50–54 hängen an der Tabelle: frische und migrierte Datenbank haben dieselbe Reihenfolge
(`db.invoices.InvoiceCol`). Der Bestand behält überall NULL.

**Fassungen und Berichtigen:** Eine versendete Rechnung hat gesperrte Finanzangaben (`db/invoice_lock.py`);
Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und Leistungsdatum ändert nur eine **Berichtigung**
(`Database.correct_invoice`, `db/invoice_versions.py`): Die Dateien der bisherigen Fassung wandern nach
`invoices/<Jahr>/Fassungen/<ID>/` (Tabelle 14a), jedes geänderte Feld steht im Protokoll (14b), und
`DocumentVersion` zählt um eins hoch. Zurückholen auf Entwurf legt die Dateien ebenso ab (und zählt hoch, wenn
eine Datei abgelegt wurde). Die berichtigte Fassung trägt einen Hinweis (PDF-Zeile, CII BT-22, UBL `cbc:Note`),
der sich aus dem Stand dieser Tabellen ergibt (`Database.correction_notice`).

**Skonto und Pflichtangaben der E-Rechnung (seit 2.3.5):** Beide Dokumente (PDF mit eingebetteter CII, UBL der XRechnung)
entstehen aus demselben Modell (`export/einvoice_model.py`, `build_einvoice`):

- **Skonto:** `parse_skonto` liest `SkontoDays`/`SkontoPercent` (Prozent über die Textform als Decimal, denn die Spalte ist REAL).
  Das PDF druckt nach dem Zahlungshinweis einen Absatz (Fristende, Abzug, Zahlbetrag; der Abzug wird in einem Schritt auf den Cent
  gerundet, Grundlage ist das gedruckte Brutto, `skonto_amount`); CII (`ram:SpecifiedTradePaymentTerms/ram:Description`) und UBL
  (`cac:PaymentTerms/cbc:Note`) tragen als erste Zeile `#SKONTO#TAGE=7#PROZENT=2.00#` (BR-DE-18), dann einen Zeilenumbruch und den
  Freitext. Eine Stornorechnung hat nie Skonto, ebenso kein Beleg über höchstens 0 (Gutschrift, 0-€-Rechnung: nichts abzuziehen, und
  das ist keine Lücke). Beim Speichern einer noch nicht eingefrorenen Rechnung lehnt der Server unbrauchbare Angaben ab (eine Angabe
  allein, Prozent außerhalb 0–100 oder mit mehr als zwei Stellen, Tage keine ganze Zahl von 1 bis 365, Skonto bei einem Betrag von
  höchstens 0, ein Rechnungsdatum, aus dem sich keine Frist rechnen lässt, Frist nach der Fälligkeit). **Geprüft wird, bevor etwas in
  eine Zahl umgewandelt wird:** Tage sind ein `int` (kein `bool`), ein endlicher ganzzahliger `float` oder ein Text aus 1 bis 3
  ASCII-Ziffern, Prozent eine endliche Zahl oder ein Text der Form `12` bzw. `2.5` – ein Wert wie `1e9999999` kehrt in Millisekunden
  zurück (`int()` darauf über `Decimal` rechnete minutenlang). Ein unlesbarer gespeicherter Wert (Altbestand, SQL-Konsole) ergibt kein
  Skonto im Modell, dafür eine Lücke. Endet die Skontofrist eines Altbestands nach der Fälligkeit, entfällt im PDF der Satz „Ohne Abzug
  zahlbar bis zum …“ (das Dokument widerspricht sich nicht).
- **Pflichtangaben:** Ein UBL entsteht nur bei Leitweg-ID oder Käuferreferenz (BT-10; eine Bestellnummer allein löst keines aus) und
  nur, wenn `einvoice_gaps` nichts findet: Leitweg-ID, IBAN, PLZ/Ort und Kontakt (Name, Telefon, E-Mail) der eigenen Firma, PLZ/Ort und
  E-Mail des Kunden, Steuernummer oder USt-IdNr. der eigenen Firma, lesbares Skonto und ein Zahlungshinweis ohne `#` (das Zeichen ist
  in der XRechnung der Skonto-Angabe vorbehalten, BR-DE-18; Bereich `xrechnung`: Mustang meldet die eingebettete CII mit `#` als
  gültig, die XRechnung-CII-Regel BR-DE-18 nur als Hinweis; gemessen an den Regeln der XRechnung 3.0 und EN 16931). Das PDF mit der CII
  entsteht immer; `generate_invoice_documents` nennt in `error`, was fehlt (zwei Sätze, keine Angabe
  doppelt: „XRechnung (XML) nicht erstellt – es fehlt: …“ nennt, was allein dem UBL fehlt, und nur, wenn eines entstehen soll; „Die
  E-Rechnung im PDF ist unvollständig – es fehlt: …“ nennt, was auch die CII braucht; `incomplete` kennzeichnet eine solche Meldung
  gegen einen Fehler beim Erzeugen), und entfernt ein früher abgelegtes UBL eines Entwurfs (nie ein eingefrorenes oder eines in
  „Fassungen“): den gespeicherten Pfad UND die Datei am Namensschema (sonst fände der Download das alte UBL über den Rückfall
  weiter), nur Dateien mit der Endung `.xml` und nie die PDF der Rechnung (`XMLPath` lässt sich per SQL auf sie biegen; dieselbe Sicherung
  steht in `drop_stale_file`). Die Rechnungsseite zeigt die Lücken in einer Zeile und fragt vor dem Versenden nach – beides nur, solange sich die
  Rechnung noch ändern lässt (Entwurf, abgeschlossen).
- **Steuernummer und USt-IdNr.** stehen in `SellerVATID` (und im Kontakt) in einer Spalte: `tax_id_kind` ordnet sie ein. Eine
  vorangestellte Bezeichnung („USt-IdNr.“, „USt-ID“, „UID“, „VAT“, „VAT-ID“, „Umsatzsteuer-Identifikationsnummer“, „St.-Nr.“,
  „Steuernummer“ …; ohne Rücksicht auf Groß-/Kleinschreibung, mit oder ohne Punkte, Bindestriche und Doppelpunkt) fällt weg. Es ist
  eine USt-IdNr., wenn der Rest nach dem Entfernen von Leerzeichen, Bindestrichen und Punkten in Großschreibung mit einem bekannten
  Länderkürzel beginnt UND der Rest zu den Nummern dieses Landes passt (Tabelle `_VAT_ID_RULES`: die EU-Länder – Griechenland als `EL` –
  und `XI`, `GB`, `CHE`, `NO`; `DE` = 9 Ziffern; `DE-123 456 789` ergibt `DE123456789`). Alles andere ist eine Steuernummer – ein
  Bundesland-Kürzel („BY 123 456 78901“), eine Registernummer („HRB 12345“), eine deutsche Nummer mit zu wenigen Ziffern – und bleibt,
  wie eingegeben (ohne Bezeichnung, außen getrimmt, innerer Leerraum zusammengezogen). Die USt-IdNr. steht als BT-31 (CII `VA`, UBL `VAT`), die Steuernummer als BT-32 (`FC`, `TAX`); der
  Kontakt springt nur bei leerer Momentaufnahme ein. Hat der Verkäufer **keine USt-IdNr., aber eine Steuernummer**, steht sie
  zusätzlich als Verkäuferkennung BT-29 (CII `ram:SellerTradeParty/ram:ID`, UBL `cac:PartyIdentification/cbc:ID`), denn BR-CO-26
  verlangt BT-29, BT-30 oder BT-31 – mit USt-IdNr. gibt es kein BT-29. Das PDF druckt die Nummer im Fuß aus der Momentaufnahme
  („USt-IdNr.: …“, „Steuer-Nr.: …“). Eine Steuernummer des Käufers entfällt (kein Feld in EN 16931).
- Der Kontoinhaber (BT-85) steht nicht in den Dokumenten; der Bankname bleibt im Fuß des PDF.

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
| PricePerUnit | INTEGER | NOT NULL | Netto, Minor Units |
| TotalNet | INTEGER | NOT NULL | Quantity × PricePerUnit, Minor Units |
| TaxCategory | TEXT | DEFAULT 'S' | |
| TaxRate | REAL | NOT NULL | |

---

### 14. InvoicePayments

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| InvoiceID | INTEGER | NOT NULL, FK → Invoices (ON DELETE CASCADE) | |
| BookingID | INTEGER | NOT NULL, FK → Bookings | Bankbewegung der Zahlung (Altbestand: auch ein Buchungssatz) |
| Amount | INTEGER | NOT NULL | Zahlungsbetrag in Minor Units (1 = 0,0001 EUR). Negativ sind die Zuordnungen des WISO-Imports (Storno, Gutschrift) und die Erstattung einer Stornorechnung (an der Stornorechnung); sonst legt die Oberfläche nur positive Beträge an |
| PaymentDate | DATE | NOT NULL | Datum der Bankbewegung |
| Notes | TEXT | | |
| CreatedAt | DATETIME | | |

Das Paar (`InvoiceID`, `BookingID`) kommt höchstens einmal vor. Jeder Schreiber
(`assign_invoice_payment`, `assign_storno_refund`, `link_invoice_to_transaction`) prüft und fügt in einer
`BEGIN IMMEDIATE`-Transaktion ein; seit 2.1.0 sichert zusätzlich der UNIQUE-Index
`ux_invoicepayments_invoice_booking` (ein `IntegrityError` heißt „bereits verknüpft“). Neue
Datenbanken bekommen ihn gleich, bestehende beim Öffnen – nur wenn sie keine Dubletten tragen; sonst
wird er übersprungen (kein Fehler), die Prüfliste „Zahlungszuordnungen“ meldet die Dubletten, und
beim ersten Start ohne Dubletten entsteht er.
Die Sätze zur Zuordnung stehen in Abschnitt 9a.

---

### 14a. InvoiceDocumentVersions (abgelegte Fassungen)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| InvoiceID | INTEGER | NOT NULL | die Rechnung (bewusst ohne FK: die Fassung überlebt das Löschen eines Entwurfs) |
| Version | INTEGER | NOT NULL | die abgelegte Fassung |
| Kind | TEXT | NOT NULL | `'pdf'` \| `'xml'` |
| RelPath | TEXT | NOT NULL | Pfad der Datei relativ zum Ordner `invoices/` des Benutzers (`/` als Trenner) |
| Sha256 | TEXT | | Prüfsumme der abgelegten Datei |
| ArchivedAt | DATETIME | DEFAULT NOW | UTC |
| Reason | TEXT | NOT NULL | `'corrected'` (Berichtigung) \| `'reopened'` (Zurückholen auf Entwurf) |

**Indizes:** UNIQUE(InvoiceID, Version, Kind). Dateien aus 2.1.2, zu denen es keine Zeile gibt, zeigt die
Rechnungsseite ohne Fassungsnummer.

### 14b. InvoiceCorrections (Protokoll)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY AUTOINCREMENT | |
| InvoiceID | INTEGER | NOT NULL | die Rechnung (ohne FK, wie oben) |
| InvoiceNumber | TEXT | | Momentaufnahme der Rechnungsnummer |
| Version | INTEGER | NOT NULL | die Fassung, die durch den Eintrag entstand bzw. gilt |
| ChangedAt | DATETIME | DEFAULT NOW | UTC |
| UserName | TEXT | | angemeldeter Benutzer (bei automatischer Anmeldung der Administrator) |
| Action | TEXT | NOT NULL | `'correct'` (Berichtigung) \| `'status'` (Statuswechsel) |
| Field | TEXT | | Spaltenname (`PaymentTerms`, `OrderNumber`, `BuyerRouteID`, `DeliveryDate`, `Buyer…` der Anschrift) bzw. `'Status'` |
| OldValue / NewValue | TEXT | | alter und neuer Wert; beim Status die Status-Schlüssel (`sent`, `draft` …) |

**Indizes:** `idx_invoicecorrections_invoice` auf `InvoiceID`. Statuswechsel schreibt `set_invoice_status` in
derselben Transaktion; was die Zahlungslogik am Status ändert, steht nicht darin. Aus dem Protokoll und
`Invoices.SentAt` ergibt sich, ob eine Rechnung schon einmal versendet war (`Database.was_sent_before`).

---

### 15. AssetCategories (AfA-Kategorien)

**Zweck:** BMF-Tabellen für Anlagegüter. Geseeded aus `seed_data/asset_categories.json` (30 Einträge).

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| Name | TEXT | NOT NULL | Kategoriename |
| UsefulLifeYears | INTEGER | NOT NULL | Nutzungsdauer |
| DepreciationMethod | TEXT | DEFAULT 'linear' | 'linear', 'degressive', 'both' (beide zulässig) |
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
| PurchasePrice | INTEGER | NOT NULL | Netto, Minor Units |
| UsefulLifeYears | INTEGER | NOT NULL | |
| DepreciationMethod | TEXT | DEFAULT 'linear' | 'linear', 'degressive' |
| SerialNumber | TEXT | | |
| Location | TEXT | | |
| Supplier_ID | INTEGER | FK → Contacts | |
| Document_ID | INTEGER | FK → Documents | |
| Booking_ID | INTEGER | FK → Bookings | |
| SaleDate | DATE | | |
| SalePrice | INTEGER | | Minor Units |
| Status | TEXT | DEFAULT 'active' | 'active', 'sold', 'scrapped' |
| Notes | TEXT | | |
| Parent_ID | INTEGER | FK → Assets | Erweiterung (Unter-Anlage): Nachträgliche Anschaffungskosten der Hauptanlage; sie zählt zu ihr, solange sie keine eigene Zeile in `AssetDepreciations` hat und die Hauptanlage einen Plan haben kann (siehe „AfA-Plan der App“) |
| CreatedAt | DATETIME | DEFAULT NOW | |

---

### 17. AssetDepreciations (AfA-Pläne)

| Spalte | Typ | Constraints | Beschreibung |
|--------|-----|-------------|--------------|
| ID | INTEGER | PRIMARY KEY | |
| Asset_ID | INTEGER | NOT NULL, FK → Assets ON DELETE CASCADE | |
| Year | INTEGER | NOT NULL | |
| DepreciationAmount | INTEGER | NOT NULL | AfA-Betrag in Minor Units |
| BookValue | INTEGER | NOT NULL | Restbuchwert in Minor Units |
| Booking_ID | INTEGER | FK → Bookings | AfA-Buchung (siehe „AfA-Buchung“ bei Bookings) |
| Status | TEXT | DEFAULT 'planned' | 'planned' (geplant), 'posted' (vom Programm gebucht), 'booked' (WISO-Import: der AfA-Satz der WISO-Datenbank steht in den Büchern, ohne Booking_ID) |
| BookedAt | DATETIME | | |

**Indizes:** UNIQUE(Asset_ID, Year)

**Business Logic:** Je Anlage und Jahr gibt es höchstens eine AfA-Buchung: `book_depreciation`
lehnt ein Jahr ab, dessen Buchung es gibt (`Booking_ID` zeigt auf eine vorhandene Zeile in
`Bookings`) oder das den Status `booked` hat (`ValueError`, Prüfung und Anlegen in einer
Transaktion). „Gebucht“ heißt also: die Buchung gibt es. Eine Zeile `posted`, deren Buchung
fehlt (z. B. per SQL-Konsole gelöscht), gilt als nicht gebucht – die Anlagenseite bietet das
Buchen wieder an und `book_depreciation` überschreibt die Zeile. Wird die Buchung über die
Anwendung gelöscht (`delete_transaction`, ebenso die SQL-Vorlage „Zeitbereich löschen“), geht
die Zeile auf `planned` zurück. (Die Vorlage läuft im Schreibmodus der SQL-Konsole in einer Transaktion mit
Fremdschlüsseln; Buchungssätze außerhalb des Zeitraums, die unter einer Bankbewegung des Zeitraums hängen, hängt sie ab –
`ParentBooking_ID` leer, ein Status `resolved` zurückgesetzt –, statt sie zu löschen.) `delete_asset` lehnt ab, solange eine AfA-Buchung existiert
oder Unteranlagen existieren; `booked`-Zeilen aus dem WISO-Import (ohne Buchung) hindern das
Löschen nicht. Eine Erweiterung, die zur Hauptanlage zählt (siehe „AfA-Plan der App“), lehnt es außerdem ab, sobald die
Hauptanlage seit dem Jahr der Erweiterung (nie vor ihrem Kaufjahr) ein gebuchtes Jahr hat – hier zählt auch ein als `booked`
importiertes Jahr –, weil die Erweiterung in diesen Buchungen steckt; ohne ein solches Jahr lässt sie sich löschen.

**WISO-Datenbank-Import, neu angelegte Sätze und Bestand** (seit 2.3.2; `_insert_wiso_bookings`): Der Import ist für einen **leeren Bestand**
und für **eine** WISO-Datenbank je Bestand gedacht; ist `Bookings` nicht leer (`Database.wiso_import_stock`: `bookings`, `wiso_rows`), zeigt das
Formular auf *Sonstiges* einen Hinweis und ein Pflicht-Kästchen, und `handle_wiso_fdb_import` importiert ohne Bestätigung
(`confirm_existing` = `1`) nichts; die Datenbankmethode `import_wiso_fdb` selbst prüft das nicht. Ein weiterer Import erkennt einen Satz an
seiner Quellgruppe wieder. Legt WISO Buchungen neu an (neue `ACCOUNTINGID`, sonst gleicher Inhalt), bekommt jeder Satz mit **unbekannter**
Quellgruppe eine der **verwaisten** Zeilen (`entry` mit Quellgruppe, die in keinem Satz der Datei vorkommt – auch nicht in einem künftigen
AfA-Satz; die Datei ist immer die ganze WISO-Datenbank). Die Zuordnung steht vor dem Schreiben fest und hängt nicht von der Reihenfolge der
Sätze in der Datei ab (Durchgänge über alle Sätze in fester Ordnung nach Inhalt, jede Zeile höchstens einmal). Immer nötig: gleiches Datum,
Konto, Gegenkonto und **gleicher Steuersatz**; dann (1) gleicher Betrag und gleiche, nicht leere Beleg-Nr. (der Text darf abweichen, die
Zeile mit gleichem Text geht vor), (2) gleicher Betrag, beide ohne Beleg-Nr., gleicher nicht leerer Text, (3) gleicher Betrag, die Zeile ohne
Beleg-Nr. und der Satz mit einer in WISO nachgetragenen, gleicher nicht leerer Text (die Zeile behält ihre leere Beleg-Nr.), (4) nur Altbestand:
Zeile `mirror_legacy` mit dem umgekehrten Betrag, sonst wie (1) bis (3), nicht wo die Datei selbst einen Satz mit dem Betrag der Zeile hat. Ein
Treffer ist kein neuer Satz: Nur `SourceGroup` wird gesetzt (Replay-Log), jede andere Spalte bleibt (`regrouped_rows`, bei geändertem Text
zusätzlich `regrouped_text_differs`); die Zeile gilt für den Rest des Laufs wie eine in ihrer bekannten Gruppe wiedererkannte (auch für den
AfA-Plan). Was übrig bleibt – in WISO gelöscht oder mit anderem Inhalt neu angelegt –, meldet der Lauf (`orphaned_rows_count`, die ersten 50 in
`orphaned_rows`) und die Seite *Sonstiges*; es zählt in den Auswertungen weiter mit, gelöscht wird nichts. Eine Änderung innerhalb einer
weiter bestehenden Buchungsgruppe (anderer Betrag, entfallene Teilbuchung) bemerkt der Import nicht (Details und Grenzen: `PARSER_README.md`).

**WISO-Datenbank-Import** (`import_wiso_fdb`, `_insert_wiso_bookings`, `_insert_wiso_assets`): WISO schreibt den
ganzen AfA-Plan sofort als Buchungssätze, auch für künftige Jahre. Der Import legt einen AfA-Satz mit einem Datum
nach dem Importtag nie an (Stichtag = heute, `today`; `future_depreciations_skipped` im Ergebnis); ein Satz, den ein
früherer Import (bis 2.2.4) schon geschrieben hat – auch einer, den der Nutzer geändert hat –, bleibt unverändert, und
sein Jahr gilt als gebucht (`future_depreciations_kept`). Als AfA-Satz gilt ein Satz mit `INVENTORYID` und `INVENTORYAMORTID`, mit WISOs
Buchungsart (`ACCOUNTINGKIND` 28) oder – wenn er die Spalte nicht kennt – mit Datum und Betrag einer Zeile der
AfA-Tabelle. **Der Status eines Planjahrs folgt der Buchung, nicht dem Datum:** `booked`, wenn der AfA-Satz der
Anlage für dieses Jahr in den Büchern steht (neu angelegt, aus dem CSV-Import übernommen oder als vorhandene
Zeile seiner Gruppe wiedererkannt), sonst `planned` – dann bucht die App das Jahr im jeweiligen Jahr selbst
(`book_depreciation`; nicht automatisch). Ein bisher geplantes Jahr, dessen Satz jetzt in den Büchern steht,
wird `booked` – in der Transaktion der Buchungen und auch ohne Anlagenimport (`with_assets=False`) – und trägt als
`DepreciationAmount` den Betrag, den die **Bücher** für den Satz führen: die Wirkung der angelegten, übernommenen oder
wiedererkannten Zeile auf das Anlagekonto (`db.matching.depreciation_effect`: eine Abschreibung „Aufwand an Anlagekonto“
zählt +|Betrag|, eine Korrektur oder Zuschreibung mit dem Anlagekonto im Soll – WISO schreibt sie mit vertauschten Konten –
−|Betrag|; bei einer vom Nutzer geänderten Zeile die der gespeicherten Zeile), bei mehreren Sätzen eines Jahres die Summe,
nie unter 0 (ein Jahr, dessen Korrekturen die Abschreibung übersteigen, bleibt mit 0 gebucht) – nicht den Planwert aus WISO;
ebenso eine neu angelegte Planzeile eines gebuchten Jahres. Betrag und Buchwert einer vorhandenen Planzeile aktualisiert der
Re-Import nur, solange sie `planned` ist; eine Zeile `booked` bleibt, wie sie ist (Buchwert, Status) – auch wenn WISO den
Betrag des Jahres inzwischen ändert oder den Satz nicht mehr liefert. Einzige Ausnahme: Steht der Satz in diesem Lauf in den
Büchern und weicht der Betrag einer Zeile `booked` (nicht von der App gebucht) von dem der Bücher ab – Bestand aus einem Import
bis 2.2.5, der den Planwert aus WISO trägt –, gleicht der Buchungsschritt sie an die Bücher an (nur bei Abweichung, im
Replay-Log wie das Anheben). Fehlt der
Satz in einer bekannten Quellgruppe, die mehrere Jahre trägt (der Erstlauf hat nur die vergangenen angelegt),
wird er nur nachgetragen, wenn die Gruppe für das Planjahr keine Zeile hat, die Anlage in der App eindeutig zugeordnet
ist **und** ihre Planzeile dieses Jahres existiert, `planned` ist und nicht von der App gebucht; sonst nie (ein Satz
einer bekannten Gruppe wird nicht neu angelegt – der Nutzer kann ihn gelöscht haben). Gleicht einem AfA-Satz keine Zeile
seiner Gruppe genau, hat die Gruppe aber eine freie Zeile mit einem Datum im Planjahr, hat der Nutzer ihn geändert: Er
steht in den Büchern, das Jahr gilt als gebucht, und es wird nichts angelegt (erst bekommt jeder Satz seine genaue Zeile,
jede Zeile dient höchstens einem Satz). Hat die App ein Jahr schon selbst gebucht (`Booking_ID`
zeigt auf eine vorhandene Buchung), bleibt die Planzeile unverändert, und der AfA-Satz der WISO-Datenbank für dieses
Jahr wird nicht angelegt (`depreciation_conflicts`, Seite *Sonstiges*). Zugeordnet wird die Anlage über
die Inventarnummer (`Assets.InventoryNumber` = `INVNO`), nur wenn sie in der WISO-Datenbank eindeutig ist; sonst
steht eine Warnung in `asset_warnings`, und die AfA-Sätze der Anlage nehmen am Abgleich nicht teil. Der Buchungsschritt
liest die Planjahre der App in seiner schreibenden Transaktion (`BEGIN IMMEDIATE`): Zwischen Lesen und Schreiben bucht
keine andere Anfrage ein Jahr. Eine vorhandene Anlage behält beim Re-Import ihr Anlagekonto (`COA_ID`).
`book_depreciation` rechnet mit dem Plan der App (`depreciation_plan`, siehe unten) und überschreibt den WISO-Betrag
der Planzeile: Weicht ein geplantes Jahr um mehr als 1,00 € vom WISO-Betrag ab oder plant die App dafür nichts, meldet der
Import das je Anlage (`depreciation_plan_mismatches`, Warnung in `asset_warnings`; der Behelfsbetrag bis 1 € in WISOs
`PURCHASEAMOUNTNET`, der nicht zum Anschaffungswert zählt, erklärt kleinere Abweichungen). Plant WISO für eine nicht
abgegangene Anlage kein Jahr mehr (alle Sätze stehen in den Büchern), die App aber noch ein offenes Jahr über 1,00 €,
meldet der Import den Restbuchwert (`depreciation_rest_warnings`, Warnung in `asset_warnings`). Bestand aus einem Import bis
2.2.4 (künftige AfA-Buchungen, Jahre `booked`) wird nicht angefasst. Der Anschaffungswert einer importierten Anlage ist die
Summe der positiven Zahlungen plus der eingetragene Wert (`PURCHASEAMOUNTNET`), wenn dieser über 1,00 € liegt und nicht
auf den Cent der Zahlungssumme gleicht (dann ist dieselbe Anschaffung doppelt erfasst: sie zählt einmal); ohne Zahlungen
gilt der eingetragene Wert wie bisher. Zählt der eingetragene Wert zusätzlich zu den Zahlungen, nennt eine Warnung in
`asset_warnings` die Zusammensetzung des Anschaffungswerts (Details: `PARSER_README.md`). Grenze: Ändert der Nutzer eine
**importierte** AfA-Buchung nachträglich, folgt der Plan nicht – zwischen Planjahr und importierter Buchung gibt es keine
Verknüpfung (`Booking_ID` schreibt nur `book_depreciation`).

**AfA-Plan der App** (`Database.depreciation_plan`, `db/assets.py`; `calculate_depreciation_plan` ist die reine Formel aus
Preis, Datum, Dauer und Methode und bleibt sein Baustein): Gebuchte Jahre (`_DEPRECIATION_BOOKED`: Status `booked` oder
eine vorhandene Buchung) sind Geschichte, und **gebucht ist, was in den Büchern steht**: Zeigt die Planzeile über
`Booking_ID` auf eine vorhandene Buchung (die App hat das Jahr gebucht), ist der gebuchte Betrag die Wirkung dieser Buchung
auf das Anlagekonto (`depreciation_effect`: „Aufwand an Anlagekonto“ zählt +|Betrag|, auch nach einem Kontentausch mit
negiertem Betrag; dreht der Nutzer die Buchung zur Rückbuchung um, zählt sie negativ, und das Jahr gilt mit 0 als gebucht) –
ändert der Nutzer die Buchung im Editor, folgt der Plan –, bei einem importierten Jahr (Status `booked`, keine Buchung)
`DepreciationAmount`. Gibt es keine gebuchten Jahre, oder gleicht jedes gebuchte Jahr auf den Cent dem Jahr der Formel,
ist der Plan genau diese Formel (linear, degressiv, GWG) – für eine nur in der App geführte Anlage ändert sich nichts.
Weicht die Geschichte ab (WISO-Import, nachträglich geänderter Anschaffungswert oder Betrag einer Buchung), gilt die
**Fortschreibung**: Die gebuchten Jahre stehen mit ihrem gebuchten Betrag (Methode „gebucht“), ihr Buchwert rechnet
fortlaufend vom heutigen Anschaffungswert (Ende = Anschaffungswert minus die gebuchten Beträge bis einschließlich des
Jahres, nie unter 0; Anfang = Ende plus Betrag) – der gespeicherte `BookValue` der Planzeile geht nicht ein, damit das
erste offene Jahr anschließt und der Buchwert nach einer Preisänderung stimmt. Der Restbuchwert (Anschaffungswert minus
alle gebuchten Beträge) verteilt sich linear auf die Monate vom ersten offenen Jahr (das Jahr nach dem größten
gebuchten, mindestens das Kaufjahr) bis zum Planende (Kaufmonat plus Nutzungsdauer): je Jahr
`Rest × Monate / Gesamtmonate`, ganzzahlig in Cent kaufmännisch gerundet, das letzte Jahr bekommt den Rest (nie negativ).
Nicht gebuchte Jahre zwischen gebuchten haben keine eigene Zeile, ihr Betrag steckt im Rest; ein Rest ≤ 0 ergibt keine offenen
Jahre, nach abgelaufener Nutzungsdauer kommt der ganze Rest ins erste offene Jahr. Ein offenes Jahr, das auf 0 Cent rundet,
entfällt (die Summe bleibt der Rest; es gäbe nichts zu buchen), buchbar ist das erste verbleibende. GWG-Regel und degressive
Rechnung gelten in der Fortschreibung nicht. Fehlt Preis, Kaufdatum oder Nutzungsdauer, bleiben nur die gebuchten Jahre. **In der
Fortschreibung ist nur das erste offene Jahr buchbar** (`bookable` im Plan, die Detailseite zeigt den Knopf nur dort):
Ein späteres Jahr zuerst zu buchen machte die Jahre davor zu Lücken, und ihr Betrag wanderte in die späteren Jahre;
`book_depreciation` lehnt es mit „AfA {Jahr} lässt sich erst buchen, wenn {erstes offenes Jahr} gebucht ist.“ ab. Für eine
nur in der App geführte Anlage gilt jede Reihenfolge. `book_depreciation`
liest den Plan erst in seiner Transaktion (nach `BEGIN IMMEDIATE`) und nimmt Betrag und Buchwert daraus; ein Jahr, das im
Plan nicht vorkommt, lehnt es ab. Nach dem Buchen des ersten offenen Jahres bleiben die übrigen offenen Jahre bis auf die
Rundung gleich: Gemessen an Stichproben (Reste ab 100 €) ändert sich ein mittleres Jahr um höchstens 1 Cent, das letzte Jahr
(es nimmt den Rest und trägt die Rundungsunterschiede) um höchstens so viele Cent, wie offene Jahre davor liegen.

**Abgang:** Der Plan endet mit dem Abgangsjahr (`Assets.SaleDate`, die ersten vier Zeichen): Nicht gebuchte Jahre danach
entfallen – in der Unverändert-Regel wie in der Fortschreibung –, gebuchte bleiben; das Abgangsjahr selbst steht, wie es
ist (die zeitanteilige AfA im Abgangsjahr plant die App noch nicht). `book_depreciation` lehnt ein Jahr nach dem Abgang
mit dem Abgang als Begründung ab, und `get_book_value_at_date` liefert 0, sobald der Abgang erreicht ist (der Abgang hat den
Rest ausgebucht): Lässt sich `SaleDate` als ISO-Datum lesen, am Abgangstag selbst und danach; lässt es sich nicht als ISO-Datum
lesen, beginnt aber mit einem Jahr (die ersten vier Zeichen, wie beim Ende des Plans), ab dem Jahr nach dem Abgangsjahr – im
Abgangsjahr gilt noch der Planwert; ohne lesbares Jahr gilt kein Abgang.

**Erweiterungen** (seit 2.2.7; `Assets.Parent_ID`): Nachträgliche Anschaffungskosten erhöhen den Anschaffungswert der
Hauptanlage, statt eine eigene Anlage mit eigenem Plan zu sein – die App rechnet es so (ob das steuerlich passt, klärt das
Steuerbüro). Eine Unter-Anlage **zählt** zur Hauptanlage (`Database.counts_toward_parent`), wenn die Hauptanlage existiert
und einen Plan haben kann (lesbares Kaufdatum, Preis über 0 Cent, Nutzungsdauer über 0), die Unter-Anlage **gar keine eigene
Zeile in `AssetDepreciations`** (gleich welchen Status) und keine eigenen Unter-Anlagen hat und ihr Anschaffungsdatum ein Datum
und ihr Preis größer 0 ist. „Keine Zeile“ statt „kein gebuchtes Jahr“: Löscht der Nutzer die einzige AfA-Buchung einer
Unter-Anlage, bleibt ihre Planzeile als `planned` stehen – sie bleibt eine eigene Anlage und lässt sich neu buchen, statt dass
ihr Betrag in die Zukunft der Hauptanlage wandert. Eine Erweiterung hat keinen eigenen Plan (`depreciation_plan` leer,
`get_book_value_at_date` 0), `book_depreciation` lehnt sie ab (die AfA läuft über die Hauptanlage), `sell_asset` lehnt ihren
Abgang ab (die Anlagenseite zeigt das Formular nicht), `handle_update_asset` lehnt einen Status ungleich `active` ab (nichts
wird geschrieben), und die Anlagenseite verweist auf die Hauptanlage; Nutzungsdauer, Methode und Anlagekonto der Hauptanlage
gelten – gebucht wird allein an der Hauptanlage. Eine Unter-Anlage mit eigener AfA-Zeile (Bestand, WISO-Import) oder unter einer
Hauptanlage ohne Plan bleibt eine eigene Anlage mit ihrem Plan (wie bis 2.2.6) und fließt nicht in die Hauptanlage; fehlen ihre
Planzeilen, zählt sie wieder. Maßgeblich ist das
Anschaffungsdatum der Erweiterung – der Tag, an dem die Kosten entstehen, nicht die Zahlung –; ihr Jahr ist nie früher als
das Kaufjahr der Hauptanlage. Im Jahr der Erweiterung zählt sie **wie zu Jahresbeginn aufgewendet** (keine Kürzung nach
Monaten): Der Restbuchwert plus Erweiterung verteilt sich linear nach Monaten auf die Restnutzungsdauer der Hauptanlage.
**Ausnahme Anschaffungsjahr:** Fällt die Erweiterung ins Kaufjahr der Hauptanlage, rechnet die Formel auf Anschaffungswert
plus Erweiterung, ab dem Kaufmonat der Hauptanlage. Der erwartete Plan ist die Formel auf den Anschaffungswert samt den
Erweiterungen des Kaufjahrs bis zum Jahr vor der ersten späteren Erweiterung; ab diesem Jahr verteilt er den Rest linear
nach Monaten bis zum Planende (auch bei degressiver Methode oder GWG der Hauptanlage – dieselbe Vereinfachung wie in der
Fortschreibung), jede weitere Erweiterung kommt in ihrem Jahr zum Rest, und liegt eine Erweiterung nach dem Ende der
Nutzungsdauer, steht ihr ganzer Betrag im Jahr der Erweiterung. Die Unverändert-Regel vergleicht die gebuchten Jahre mit diesem
Plan. Entsteht eine Erweiterung in einem Jahr, das schon gebucht ist, weicht die Geschichte ab: Fortschreibung, ihr Betrag
steckt im Rest ab dem ersten offenen Jahr, und die Buchwerte der gebuchten Jahre rechnen vom Anschaffungswert samt
Erweiterungen bis zu diesem Jahr. Ohne Erweiterungen ändert sich nichts. `Database.acquisition_cost(asset_id, year)` ist der
Preis der Anlage plus die zählenden Erweiterungen mit Jahr ≤ `year` (Standard: laufendes Jahr); die Anlagenliste zeigt ihn als
Anschaffungskosten. Die Anlagenseite legt keine Erweiterung einer Erweiterung an (`handle_add_asset` lehnt ab).
**Löschen:** Eine zählende Erweiterung lässt sich nicht löschen, sobald die Hauptanlage seit dem Jahr der Erweiterung (nie vor
ihrem Kaufjahr) ein gebuchtes Jahr hat (`_DEPRECIATION_BOOKED`, auch ein importiertes) – die Erweiterung steckt in diesen
Buchungen (`delete_asset`: „Die Hauptanlage hat seit dem Jahr dieser Erweiterung AfA gebucht (…)“); ohne ein solches Jahr schon,
und der Plan der Hauptanlage rechnet wieder ohne sie.

---

## Datenfluss

### Bank↔Entry-Verknüpfung

```
1. WISO-Tabellen-Export  → Bookings mit BookingType='bank' (Bankbewegungen)
2. WISO-Original-Export  → Bookings mit BookingType='entry' (Buchungssätze)
   (inkl. TaxRate aus den Schlüsselregeln bzw. TaxKeys, `DatevKey` = SCHLUESSEL, TaxAmount berechnet)
3. link_bank_to_entries() verknüpft automatisch:
   Richtungsregel (seit 2.3.14, Stufen 1–6): Verknüpft wird nur, wenn die Wirkung des Satzes auf das Konto
   der Bankbewegung – mit Vorzeichen, gerechnet wie die Split-Summe (`matching.link_effect`, `bank_effect`) –
   dem Betrag der Bewegung entspricht; bei einer Gruppe die Summe der Wirkungen. Eine Lastschrift wird nie mit
   einer Einnahme verknüpft, ein Satz mit anderer Geldseite (Kasse, anderes eigenes Bankkonto, 1460) oder dem
   Konto (`Account_ID`) einer anderen Bewegung nie, ein Alt-Spiegel `mirror_legacy` nie automatisch.
   Umbuchungen ohne Geldseite zählen bei der Eindeutigkeit mit, werden aber nie allein verknüpft.
   Reihenfolge im Lauf: 0 (Quellklammer), 1, 2, 4, 3, 3b, 3c, 3d, 5, 6, 6b, Nachlauf, 7.
   - Stufe 1: Datum + normalisierter Empfänger + Betrag
   - Stufe 2: Datum + Betrag (eindeutig nach Doppik-Filter)
   - Stufe 3: Split-Gruppen mit Summenabgleich (nur gleicher Tag)
   - (WISO Tabellen-Import Matching: Stufe A: bank+entry-Paar ohne Belegnummer —
     identisches Datum/Betrag/Text, entry ist Child der bank-Buchung)
   - Stufe 3b: Rechnungs-Split (SUM/Anzahl, Bank-COA als Marker)
   - Stufe 3c: Privatanteil-Split (Summe minus Privatentnahme-Offset)
   - Stufe 3d: Sammelzahlung (mehrere Rechnungsnummern im Bank-Text)
   - Stufe 4: DocumentNumber als Tiebreaker bei Mehrdeutigkeit
   - Stufe 5: Text-Token-Matching (lange Ziffernfolgen >= 8 Stellen)
   - Stufe 6: Text-Similarity ohne Belegnummer (SequenceMatcher)
   - Stufe 7: Debitoren-Auflösung (Status='resolved' für Debitoren-Entries
     deren Zahlung bereits verknüpft ist, datumsunabhängig)
   Forderungsbuchungen (Debitoren 10000 ohne Zahlungskonto) werden in keiner Stufe
   verknüpft (außer über die Quellklammer, Stufe 0) – sie gehören zur Rechnung
   (siehe 9a, `receivable_condition`)
4. Entry.ParentBooking_ID → Bank.ID
5. Doppik-Entries (COA_ID = Bankkonto-SKR) werden ausgeblendet
```

### Beleg lesen und Buchung vorschlagen (seit 2.6.0)

```
1. Upload (/receipts/upload) legt den Beleg an (Documents, Datei unter Belege/<Jahr>/) – der Upload liest nicht
2. Die Seite fragt je Beleg POST /receipts/read: receipt_reader.read_receipt öffnet die Datei (receipt_files.open_receipt), liest die Bytes
   und gibt sie an einen Kindprozess (receipt_reading.worker) – außerhalb jeder Transaktion. Zuerst die E-Rechnung (XML oder das XML in
   einer PDF), dann die Textregeln einer gewöhnlichen PDF; Scans und Bilder werden nicht gelesen (kein OCR)
3. Database.save_document_reading → DocumentReadings (eine Zeile je Beleg, nur wenn Beleg und Pfad unverändert sind); Documents bleibt unberührt
4. Seite des Belegs und GET /receipts/suggestions?id= → Database.suggest_bookings_for_receipt (rein lesend):
   - Voraussetzung: Lesung `ok`, Währung leer oder EUR, GrossMinor ≠ 0; maßgebliches Datum: DocDate, sonst Documents.Date
   - hängt der Beleg an irgendeiner Buchung (eine Zeile in BookingDocuments, gleich welcher Art), ist die Liste leer – geprüft vor den Kandidaten;
     nach dem Lösen der letzten Verknüpfung kommen die Vorschläge wieder
   - receipt_match_candidates: Bankbewegungen und eigenständige Sätze mit |Amount| = |GrossMinor|, Buchungsdatum von 7 Tagen vor bis 90 Tage
     nach dem Datum – ohne Kinder einer Bankbewegung, Zahlungssätze (invoice_payment), Spiegel, Forderungen einer eigenen Rechnung und
     Buchungen, an denen dieser Beleg schon hängt (auch über ein Kind); höchstens 200, älteste zuerst
   - receipt_matching.rank_candidates (rein): Punkte für die Rechnungsnummer im Text, in der Beleg-Nr. oder im Empfänger (100), gemeinsame
     Namenswörter (30, ab zwei 40) und ein Buchungsdatum nahe dem Belegdatum (bis 20); die Zahlungsrichtung nach der Belegart (Rechnung: Betrag < 0,
     Gutschrift: Betrag > 0) – eine unpassende steht hinter den passenden, mit eigener Begründung, ausgeschlossen wird sie nie; Buchungen, die schon
     einen Beleg tragen, zuletzt (Sortierung: verknüpft, Richtung unpassend, −Punkte, Abstand, ID); höchstens 5, je mit festen Begründungen (der
     Betrag zuerst)
5. „Zuordnen“ → POST /bookings/link-document (BookingDocuments; die Buchung übernimmt unter den Bedingungen aus Abschnitt 10 die Beleg-Nr.) –
   der einzige Schritt, der etwas verknüpft
```

### EÜR-Ableitung (Dashboard)

Die EÜR-Werte werden aus `Bookings` abgeleitet (nicht aus Rechnungen):

- Betriebliche Kontensalden werden netto aggregiert (`Amount - TaxAmount`)
- Das Zweckkonto folgt der gespeicherten Form des Satzes (`Bookings.EntryForm`, siehe „Form
  eines Buchungssatzes“): `liquid_first` → Gegenkonto, `purpose_first` → Konto, `transfer` → beide
  Seiten, Spiegel (`mirror_*`) → nichts; ein Satz ohne liquide Gegenseite zählt als Zahlung, nicht
  als Umbuchung
- Virtuelles USt-Konto 3806 enthält nur Steueranteile aus Einnahmekonten
- Virtuelle Vorsteuerkonten 1401/1406 enthalten Steueranteile aus Ausgabenkonten
- Konten 3160, 3720, 3740 werden im Dashboard separat als "Sonstige Ausgaben" gezeigt
- Einnahme, Ausgabe, „Sonstige Ausgabe“ oder gar nicht: Das entscheidet je Konto `db.reporting.euer_section` – für die EÜR im
  Dashboard und für die betriebswirtschaftliche Auswertung (`db/bwa.py`, seit 2.5.14, Seite `/bwa`), die dieselben Zahlen von
  `get_euer_data` (ohne Kontofilter) nur nach Gruppen zusammenfasst (`RANGES`, `EXCEPTIONS`; ein Konto ohne Gruppe steht unter
  „Nicht zugeordnet“) und nichts aus `Bookings` selbst liest
- Der Kasten der EÜR im Dashboard und die Seite `/euer` (seit 2.5.16, auch als PDF) kommen aus EINER Aufbereitung, `db.euer.euer_statement`
  (die Zeilen von `get_euer_data` nach `euer_section`; Summen aus Minor Units). Die Seite hat keinen Kontofilter; der Kasten folgt dem Filter des Dashboards
- **Liquide Konten** (`get_payment_coa_ids`): eigene Bank-/Kassenkonten (die Standard-Kasse trägt 1600) und das Verrechnungskonto 1460 – nur noch für Zeilen ohne gespeicherte Form (Roh-SQL), die die EÜR damit einordnet. Ob ein Satz zählt, entscheidet die Sektion in `get_euer_data()`. Jeder Satz zählt in genau einer Sektion: Die Sammlung ist über die Buchungs-ID eindeutig, die erste Sektion, die einen Satz findet, gewinnt (Reihenfolge 1, 2, 2b, 3); außerdem sind die Sektionen so geschnitten, dass sich die üblichen Fälle gar nicht erst überschneiden:
  - **Section 1:** Buchungssätze (Kinder) unter einer Bankbewegung der gewählten Konten – auch der Kasse (Buchungsmaske mit Konto = Kasse; die Bankzeile trägt `Account_ID` der Kasse)
  - **Section 2:** eigenständige Sätze (ohne `ParentBooking_ID`), die ein Kassenkonto berühren, und – nur ohne Kontofilter – das Verrechnungskonto 1460 (WISO: „Aufwand an Verrechnung“ gilt mit dem Datum des Satzes als abgeflossen; die spätere Übertragung „Verrechnung an Bank“ ist ein Geldkonten-Spiegel und zählt nicht noch einmal)
  - **Section 2b (Kasse):** eigenständige Sätze mit `Account_ID` auf einer Kasse (`IsCash=1`), die in Section 2 nicht vorkommen (z. B. `CounterCOA_ID IS NULL`; ältere Einträge, Kasse ohne SKR-Nummer). Sie laufen durch dieselbe Lesart der Form wie alle Sätze (`read_entry_form`). AfA-Buchungen alter Form und Umbuchungen zwischen Anlage- und Erfolgskonto gehören nicht dazu, auch wenn die Maske ihnen eine Kasse als Konto eingetragen hat – sie zählen in Section 3 und mit einem Kontofilter „nur Kasse“ gar nicht (AfA hängt an keinem Konto). Section 2b wird in `get_euer_data()` und `get_dashboard()` (samt den Hüllen `get_dashboard_monthly/totals()`) berücksichtigt.
  - **Section 3 (nicht zahlungswirksam, nur ohne Kontofilter):** eigenständige Umbuchungen zwischen Anlagekonto (SKR04 1–999) und Erfolgskonto (4000–7999) – AfA „Aufwand an Anlagekonto“, Restbuchwert beim Abgang (`is_asset_transfer`) – und die Sätze, auf die `AssetDepreciations.Booking_ID` zeigt (auch alte AfA-Sätze). Das Anlagekonto selbst wird bei Umbuchungen nicht bebucht. Solche Sätze tragen keine Zahlung: `TaxAmount`/`TaxRate` bleiben unberücksichtigt (keine Vorsteuer auf 1401/1406). Der Anlagenkauf mit Vorsteuer als Kind einer Bankbewegung (Section 1) ist davon nicht berührt.
  - Das **Dashboard** (Zahlungskarten, `Database.get_dashboard`) folgt den Kontobewegungen: Bankbewegungen aller gewählten Konten inkl. Kasse sowie eigenständige Sätze auf Kassenkonten (Section 2/2b); das Verrechnungskonto 1460 (außer als Konto der Kasse) und die AfA zählen dort nicht. Seit 2.4.1 wird **jede gezählte Bewegung ohne Rest in vier Töpfe zerlegt** – Einnahme, Ausgabe, privat (Konten 2100–2199) und Übertrag (zwischen eigenen Konten, Bank ↔ Bank und Bank ↔ Kasse); ihre Summe ist der Saldo (`balance`), der sich dabei nicht ändert. *Bankbewegung mit Kindern:* Die Wirkung jedes Satzes ist `bank_effect` auf das Konto der Bewegung (wie in der Split-Summe). Privat ist der Betrag der Sätze mit 2100–2199 – nicht ihre Wirkung: „2100 an 1460“ wirkt auf das Konto der Bewegung nicht und bleibt trotzdem privat (eine Umbuchung `transfer`: −Betrag, wenn 2100–2199 im Konto steht, +Betrag im Gegenkonto, 0 auf beiden Seiten). Trägt die Bewegung einen Spiegel zwischen eigenen Konten (`is_mirror_row`), ist der Übertrag der Betrag minus die Wirkung aller übrigen Sätze – der Betrag des Spiegels wird nie gelesen, auch nicht bei `mirror_legacy`; der Rest (Betrag − privat − Übertrag) zählt nach dem Vorzeichen als Einnahme oder Ausgabe. *Bankbewegung ohne Satz:* ganz Übertrag, wenn die Übertragsrechnung des DATEV-Exports sie deckt (`settle_transfers`, über den ganzen Bestand), sonst Einnahme oder Ausgabe; sie zählt in beiden Fällen weiter als „offen“. *Eigenständiger Satz auf der Kasse:* Betrag aus Sicht der Kasse – ein Spiegel mit bekannter Lesart (`mirror_coa`, `mirror_counter`) zählt aus ihrer Sicht (Abhebung +, Bareinzahlung −), jede andere Zeile mit ihrem Betrag; ein Spiegel zwischen eigenen Konten (auch `mirror_legacy`, mit dem Rohbetrag) ist ein Übertrag, 2100–2199 auf einer Seite privat, alles andere nach dem Vorzeichen. Ein Übertrag auf ein nicht gewähltes Konto bleibt ein Übertrag und steht dann netto unter „Überträge“. Bekannte Grenzen (`docs/aufgaben.md`): Die Kassenseite eines Übertrags, die nur als Kind unter der Bankbewegung steht, zählt nicht; ein unkontierter Rest neben einem Spiegel steckt im Übertrag (auch ein Satz über 1460 ohne Wirkung auf das Konto); Konten ohne oder mit doppelter SKR-Nummer nehmen an der Deckung nicht teil; ein eigenständiger Spiegel des Altbestands (`mirror_legacy`) auf der Kasse deckt die Bankbewegung ohne Satz nicht; die Deckung setzt wie beim DATEV-Export vollständige Auszüge voraus.
- **Bestandskonten** (`NEUTRAL_ACCOUNT_NUMBERS`: 10000 Debitoren, Wartekonten 4340/4345/4405) bleiben komplett draußen – weder Saldo noch USt. Die Einnahme entsteht mit der Umbuchung vom Warte- auf das Erlöskonto bei Zahlung (siehe Abschnitt 9a).

### Summen- und Saldenliste (seit 2.5.17, `db/susa.py`, `Database.get_susa`)

Je Konto Anfangssaldo, Soll und Haben im Monat und aufgelaufen seit dem 1. Januar und Endsaldo (Seite `/susa`, auch als PDF). Sie liest **dieselben Zeilen wie der
DATEV-Export und mit derselben Lesart** – eine eigene Deutung von Form, Soll und Haben gibt es nicht. Rein lesend: kein `DateTax`-Stempel, nichts wird geschrieben.

- **Zeilen:** alle Sätze außer den Bankbewegungen mit Datum bis zum Monatsende (`accounts.LEDGER_ROWS_WHERE`, dasselbe `WHERE` wie `fetch_bookings_range`; die
  Sätze der Vorjahre bilden den Anfangssaldo), **ohne die Eingangs-Sätze gepaarter Überträge**, die der Export weglässt (`settle_transfers(…).omitted`, über den
  ganzen Bestand gezählt): Der Ausgangs-Satz („Bank B an Bank A“) bucht beide Bankseiten, mit dem Eingangs-Satz stünde der Übertrag doppelt da. Liegen die beiden
  Seiten in verschiedenen Monaten, verschiebt sich das nur zwischen den Monaten. Form und Soll-Seite liest `matching.ledger_form`/`matching.coa_in_soll` (der Export ruft
  dieselben Funktionen), die Art `datev_keys.booking_kind`.
- **Soll und Haben** (wie Spalte 1 des Exports; der Betrag ohne Vorzeichen):

  | Form | Betrag ≥ 0 | Betrag < 0 |
  |---|---|---|
  | `purpose_first` | Soll Gegenkonto, Haben Konto | Soll Konto, Haben Gegenkonto |
  | `liquid_first`, `transfer`, `mirror_coa` | Soll Konto, Haben Gegenkonto | umgekehrt |
  | `mirror_counter` | Soll Gegenkonto, Haben Konto | umgekehrt |
  | `mirror_legacy` | nicht gebucht (die Lesart ist unbekannt, sie wird nie gedeutet), Hinweis | – |

  Ein Satz mit Betrag 0 bucht nichts. Eine Seite ohne Konto (oder mit einem Konto ohne Nummer) steht auf dem virtuellen Konto „Nicht kontiert“. Ein Satz unter einer
  Bankbewegung, dem die Geldseite fehlt (`purpose_first` ohne Gegenkonto, `liquid_first` ohne Konto), ist das Konto der Bankbewegung (so zählen ihn EÜR und Split-Summe auch;
  hat die Bewegung keine SKR-Nummer: „Nicht kontiert“). Eine Zeile ohne gespeicherte Form wird mit den Zahlungskonten von heute gelesen wie im Export (Hinweis).
- **Steuer:** Der gespeicherte `TaxAmount` gilt, nichts wird nachgerechnet; mit dem Vorzeichen des Betrags wird er aus der Seite des Zweckkontos herausgerechnet (die Seite
  bekommt Betrag − Steuer, das Steuerkonto die Steuer auf derselben Seite). Welche Seite, entscheidet die Art (`booking_kind`): Zahlung am Zweckkonto (`purpose_first`: Konto,
  `liquid_first`: Gegenkonto), Umbuchung am Gegenkonto, wenn es ein Erlös- (Umsatzsteuer) bzw. Aufwands- oder Anlagekonto (Vorsteuer) ist, sonst am Konto; „Wartekonto an Erlöskonto“ an
  der Erlösseite, die kein Wartekonto ist. Die Forderung „Debitoren an Wartekonto“ (und die Gegenbuchung einer Stornorechnung) wird nie zerlegt – das Wartekonto hält den
  Bruttobetrag bis zur Zahlung –, ohne Hinweis. Ohne Seite (Spiegel, Debitor als Zweckkonto, Umbuchung ohne Erlös-, Aufwands- oder Anlagekonto, kein Konto auf der Seite) steht der Satz
  brutto, Hinweis `tax_not_split`. Steuerkonto je Art und Satz (`datev_keys.percent`):

  | Steuerart | 19 % | 16 % | 7 % | 5 % | anderer oder kein Satz |
  |---|---|---|---|---|---|
  | Vorsteuer | 1406 | 1406 | 1401 | 1401 | „Steuer ohne Steuerkonto“ (virtuell, Hinweis `tax_unassigned`) |
  | Umsatzsteuer | 3806 | 3806 | 3801 | 3801 | ebenso |

  16 % und 5 % liegen auf den Konten von 19 % und 7 %. Der Name kommt aus dem Kontenrahmen; wo er das Konto nicht führt, trägt es den Namen aus `TAX_ACCOUNT_NAMES` mit dem Zusatz „(nicht im
  Kontenrahmen)“. Der Standard-Kontenrahmen führt 3801 seit 2.6.1; ein älterer Bestand bekommt es über „Standardkonten ergänzen“ (`missing_standard_accounts`) und trägt bis dahin den Zusatz.
  Umsatzsteuer und Vorsteuer werden nicht verrechnet, einen Abschluss gibt es nicht.
- **Kontenklassen und Vortrag** (EINE Stelle: `GROUPS`, `group_key`, `is_carried`):

  | Gruppe | Bereich | trägt vor |
  |---|---|---|
  | Anlagevermögen | 0–999 | ja |
  | Umlaufvermögen und Finanzkonten | 1000–1999 | ja |
  | Eigenkapital und Privat | 2000–2999, dazu „Ergebnis der Vorjahre (rechnerisch)“ | ja |
  | Rückstellungen, Verbindlichkeiten und Steuern | 3000–3999, dazu „Steuer ohne Steuerkonto“ | ja |
  | Erlöse und Erträge | 4000–4999 ohne die Wartekonten | **nein** |
  | Offene Posten (Wartekonten) | 4340, 4345, 4405 | ja |
  | Aufwand und übrige Erfolgskonten | 5000–7999 | **nein** |
  | Vortrags- und statistische Konten | 8000–9999 | ja |
  | Personenkonten | ab 10000 | ja |
  | Nicht kontiert | virtuell | ja |

  Anfangssaldo: Bestandskonten tragen die Summe (Soll − Haben) aller Sätze vor dem 1. Januar vor; Erfolgskonten beginnen bei 0, ihre Summe aus den Vorjahren steht auf dem
  virtuellen Konto „Ergebnis der Vorjahre (rechnerisch)“ beim Eigenkapital – so ist die Summe aller Anfangssalden 0. Privat- und Steuerkonten werden mangels Abschluss vorgetragen. Eine
  Eröffnungsbilanz gibt es nicht: Ein Konto ohne Buchungen vor dem ersten Kontoauszug beginnt bei 0. Gleiche Nummern aus verschiedenen Kontenrahmen sind eine Zeile (Name des ersten nach
  `ChartOfAccounts.ID`; für eine Bankkontonummer ohne Zeile im Kontenrahmen der Name des Kontos).
- **Ausgleich:** Jede Buchung schreibt gleich viel ins Soll wie ins Haben, was ihr fehlt, steht auf „Nicht kontiert“: In jeder Spalte ist Summe Soll = Summe Haben, die Summe der Anfangs-
  und der Endsalden 0. **Bankbewegungen ohne Buchungssatz**, die kein Übertrag deckt (`settle_transfers(…).covered`), stehen mit dem Bankkonto gegen „Nicht kontiert“ (Betrag > 0: Bank im
  Soll), sonst stimmte der Bankstand nicht mit dem Kontoauszug; auf einem Konto ohne SKR-Nummer werden sie nur gezählt. Was sich nicht buchen lässt, steht nur in den Hinweisen – jede Zeile in genau einem:
  `unknown_type` (keine Buchungsart, `BookingType` NULL – der Export liest die Zeile nie), `bad_date` (das Datum ist kein Kalendertag als Text `JJJJ-MM-TT` ab dem Jahr 0001: `typeof = 'text'`, das Muster und `x IS date(x)`; die Zeile fehlt in jedem Monat, jede
  Buchungsart, ohne Fenster), `not_readable` (Betrag oder Steuerbetrag keine ganze Zahl bis 10^15 Minor Units, Datum gültig und bis Monatsende), `zero_amount_tax` (Betrag 0 mit Steuerbetrag – ohne Betrag keine Seite, die
  Einnahmenüberschussrechnung zählt die Steuer), `legacy_mirror` (`mirror_legacy`). Eine andere Buchungsart als `entry` und `bank` liest der Export wie einen Satz, und so bucht sie die Liste. Eine Summe über 2^63 Minor Units
  rechnet `_sum_groups` in Python.
- **Abgleich mit den Bankbewegungen:** Für jedes eigene Bankkonto (keine Kasse) mit eigener SKR-Nummer soll der Endsaldo der Liste die Summe aller Bankbewegungen bis Monatsende sein
  (`BankCheck`; die Summe liest das Datum als Text – eine Bewegung mit ungültigem Datum, das vor dem Monatsende sortiert, zählt mit und fehlt in der Liste); Konten ohne oder mit doppelter Nummer stehen in `bank_skipped`. Eine Abweichung entsteht durch Bankbewegungen, deren Sätze nicht
  ihren Betrag ergeben (`unbalanced`), Sätze auf dem Bankkonto ohne Bankbewegung, Überträge mit verschiedenen Daten beider Seiten, Sätze, die ein anderes Datum als ihre Bankbewegung tragen, wenn das Monatsende dazwischen liegt
  (`child_other_period`), Sätze unter einer Bankbewegung, die auf ein anderes eigenes Bank- oder Kassenkonto buchen (`child_other_account`; ein Spiegel zwischen eigenen Konten zählt nicht), oder nicht lesbare Werte;
  die beiden Hinweise zählen nur, an der Buchung ändert sich nichts.
- **Abstimmungsproben** (Tests, auf den Minor-Wert): (a) Summe Soll = Summe Haben, Anfangs- und Endsalden zusammen 0 – in jedem Bestand; (b) Bankkonto gegen Bankbewegungen – wo jede
  Bewegung kontiert ist, jeder Übertrag beide Seiten am selben Tag trägt, jeder Satz mit seiner Bewegung auf derselben Seite des Monatsendes liegt und auf deren Konto bucht und alle Beträge lesbar sind; (c) Erfolgskonten gegen die EÜR (für jede Zeile eines EÜR-Abschnitts außer 3806: der
  Saldo der EÜR = Haben − Soll der Liste; die Umsatzsteuer der EÜR = 3801 plus 3806) – wo jeder Satz einer Bankbewegung oder der Kasse zugeordnet ist (Sätze ohne Bankbewegung, Umbuchungen ohne
  Zahlung und Status `resolved` zählt die Liste nach ihrem Datum, die EÜR nicht).
- **Laufzeit:** Die Sätze werden in SQL nach dem gruppiert, was ihre Lesart bestimmt (Form, Konten, Richtung des Betrags und der Steuer, Steuersatz, Zeitraum, Konto der Bankbewegung), und je
  Gruppe einmal gebucht – die Regel ist linear (`db.susa.book_group`); eine Zeile je Satz in Python wäre bei 200 000 Buchungen deutlich langsamer. Die Übertragsrechnung
  (`_settle_stock`) liest den ganzen Bestand wie bei der Dashboard-Karte.

### Liquiditätsrechnung (seit 2.6.3, `db/liquidity.py`, `Database.get_liquidity`)

Vom Ergebnis der betriebswirtschaftlichen Auswertung zur Veränderung der flüssigen Mittel – ein Monat und aufgelaufen seit dem 1. Januar, im Vorjahresvergleich oder als „Monat und Jahr“ (Seite
`/liquiditaet`, auch als PDF). Sie hat **keine eigene Lesart der Buchungen** und liest nur drei Quellen: die Zeilen von `get_euer_data` (ohne Kontofilter, eingeordnet von `reporting.euer_section`; je Spanne einmal alle
Zeilen und – seit 2.6.4 – einmal mit `only_asset_transfers=True`: nur die Zeilen aus den Umbuchungen zwischen Anlage- und Erfolgskonto, die kein Geld bewegen – die Sätze, die Sektion 3 beisteuert, und die Umbuchungen dieser Art unter einer Bankbewegung (Sektion 1, ihre Wirkung auf das Konto der Bewegung ist 0); dieselbe Abfrage, dasselbe
Aufsummieren), den Bericht der BWA (`bwa.build_report`) über die vollen Zeilen und die Töpfe des Dashboards (`_dashboard_movements`: Einnahme, privat, Ausgabe, Übertrag – je Spanne ein Aufruf, so geht die Summe auf den
Minor-Wert der Karte `get_dashboard(…)['balance']` derselben Spanne auf). Neu ist nur die Abfrage `_liquidity_unbooked`; vorhandene Funktionen sind unverändert (`get_euer_data` bekam nur den Schalter, der Standard bleibt gleich).
Rein lesend, nichts wird geschrieben.

- **Zeilen** (Minor Units, jeder Wert mit seiner Wirkung auf die flüssigen Mittel – Zufluss +; `R` ist der Saldo einer EÜR-Zeile, `U` der Saldo des Kontos in den Umbuchungen zwischen Anlage- und Erfolgskonto
  (`get_euer_data(…, only_asset_transfers=True)`); zwei Zeilen gleicher Nummer – die echte und die virtuelle 3806, zwei Konten gleicher Nummer – sind je Zeitraum addiert; die Bereiche stehen als Daten in `db/liquidity.py`):

  | # | Zeile | Wert |
  |---|---|---|
  | 1 | Ergebnis (wie BWA) | das Ergebnis der BWA (`result`) |
  | 2 | Abschreibungen | −Σ U der Konten der BWA-Gruppe `depreciation` (6200–6299), positiv |
  | 3 | Anlagenabgänge zum Restbuchwert | −Σ U der Konten 4855–4858 und 6895–6898 (`DISPOSAL_RANGES`) |
  | 3a | Übrige Umbuchungen ohne Geldfluss | −Σ U jedes anderen Kontos (steht nur bei einem Wert) |
  | 4 | Darlehen und Verbindlichkeiten im Ergebnis | BWA-Gruppe `liabilities` (3000–3799 außer 3160, 3720, 3740), herausgerechnet |
  | 5 | Ergebnis vor Abschreibungen und Darlehen | 1 + 2 + 3 + 3a + 4 |
  | 6 | Anlagenzugänge | Σ R der Konten unter 1000 außerhalb des Ergebnisses (`ASSET_LIMIT`) |
  | 7 | Darlehen und Verbindlichkeiten | Σ R aller Konten 3000–3799 (`FINANCING_RANGE`; Aufnahme +, Tilgung −) |
  | 8 / 9 | Privatentnahmen / Privateinlagen | Σ R der Konten 2100–2179 / 2180–2199 |
  | 10 | Übrige Konten außerhalb des Ergebnisses | der Rest der Zeilen außerhalb des Ergebnisses (erwartet 0; steht nur bei einem Wert) |
  | 11 | Veränderung laut Überleitung | 5 bis 10 |
  | 12 | Bankbewegungen ohne Buchungssatz | `_liquidity_unbooked` (mit Anzahl) |
  | 13 | **Nicht zugeordnet** | 14 − (11 + 12), steht immer da |
  | 14 | Veränderung der flüssigen Mittel | Einnahme + privat + Ausgabe der Töpfe des Dashboards, alle Konten, **ohne den Topf Übertrag** |

  Jede EÜR-Zeile steht an genau einer Stelle: im Ergebnis (Zeile 1; Zeile 4 hebt nur ihren Anteil heraus) oder, nach Nummer, in einer der Zeilen 6 bis 10. Die Zeilen 2, 3 und 3a rechnen nur zurück, was die EÜR als Umbuchung
  zwischen Anlage- und Erfolgskonto liest (AfA, Restbuchwert beim Abgang): Sie nehmen ihre Werte allein aus den Umbuchungen (Prüfung erst `DISPOSAL_RANGES`, dann die Gruppe `depreciation`, sonst 3a), nie aus einer
  EÜR-Zeile allein. Eine direkt bezahlte Abschreibung („6260 an Bank“) bewegt Geld, bleibt im Ergebnis und steht in keiner dieser Zeilen; im Kontennachweis steht der Betrag der Umbuchungen, nicht der ganze Saldo des Kontos.
  Die Zeilen 4 und 7 heben sich bis auf 3160, 3720 und 3740 auf – die Konten 3000–3799 stehen schon im Ergebnis der BWA und kommen als Finanzierung zurück.
- **Ist ohne Überträge:** Geld zwischen eigenen Konten ändert die flüssigen Mittel nicht. Der Topf „Übertrag“ steht nur nachrichtlich da (Zeile „Überträge zwischen eigenen Konten“ und „Veränderung laut
  Kontobewegungen“ = Ist + Überträge = `balance` der Karte), und nur, wenn Überträge dastehen.
- **Bankbewegungen ohne Buchungssatz (`_liquidity_unbooked`):** Bankbewegungen der Konten mit Datum im Zeitraum und ohne einen einzigen Satz – dieselbe Auswahl wie die Zählung `unlinked` des Dashboards –, ohne die, die die
  Übertragsrechnung des Exports deckt (`_settle_stock(…).covered`; sie läuft nur, wenn eine Bewegung ohne Satz einen ganzzahligen Betrag ≠ 0 hat, und einmal je Aufruf). Beträge über `_minor_or_zero`.
- **Nicht zugeordnet:** per Definition die Differenz – keine dritte Lesart. Was die Überleitung aus den Buchungen nicht erklärt (die EÜR führt eine Zahlung in einem anderen Monat oder mit einem anderen Betrag als das Konto),
  bleibt dort sichtbar. „Geht auf“ heißt exakt 0 Minor Units; ein Rest unter einem halben Cent steht als 0,00 da und wird als Rundungsrest genannt. Grenze der Ist-Seite (die des Dashboards): Neben einem Spiegel zwischen
  eigenen Konten steckt der Teil einer Bankbewegung, den kein Satz erklärt, im Übertrag – ein unstimmiger Split dort steht nur nachrichtlich unter den Überträgen.
- **Abstimmung** (Tests, auf den Minor-Wert): Im Demobestand und in Beständen aus Zufall über Wege der App ist „Nicht zugeordnet“ in jedem Monat und aufgelaufen 0; in der SuSa-Welt und der Testwelt stehen feste Werte
  (`tests/test_liquidity.py`), und für die benannten Fälle (ein Satz über 1460 in einem anderen Monat als seine Bankseite, ein Satz mit anderem Datum als seine Bankbewegung über eine Monatsgrenze, ein unstimmiger Split, ein
  Steuersatz außer 5, 7, 16 und 19 %, der Status `resolved`, eine Zahlung auf ein Bestandskonto ohne Gegenumbuchung, eine Umbuchung Anlage ↔ Erfolgskonto mit der Kasse als `Account_ID`, ein Spiegel alter Art mit 1460,
  ein Rundungsrest) ist festgehalten, was dort landet. Es gehen auf: die direkt bezahlte Sofortabschreibung gegen die Bank, die Umbuchung „6815 an 0520“ (Zeile 3a) und ein Abgang über das Verrechnungskonto. Ein Test prüft die
  Zerlegung von `get_euer_data(…, only_asset_transfers=True)`: Saldo(alle) − Saldo(nur Umbuchungen) = Saldo eines Bestands, aus dem genau diese Sätze gelöscht sind.
- **Laufzeit:** Je Spanne zweimal `get_euer_data` (alle Zeilen und nur die Umbuchungen) und einmal `_dashboard_movements` (die Übertragsrechnung `_settle_stock` liest den ganzen Bestand), dazu `_liquidity_unbooked`. Gemessen an
  einem Bestand mit 200 000 Buchungssätzen: rund 2,5 s im Vorjahresvergleich (vier Spannen), rund 1,4 s als „Monat und Jahr“; im Demobestand `full` rund 10 bis 20 ms.

### Rechnungserstellung

```
1. NumberRanges → InvoiceNumber
2. Contacts (Type='own') → Seller-Snapshot
3. Contacts (Type='customer') → Buyer-Snapshot
4. Accounts → Bank-Snapshot
5. Articles → InvoiceItems
6. Status: draft → finalized → sent → paid (dazwischen partial_payment, overdue; cancelled)
7. Versenden bucht die Forderung; Zahlungen lassen sich ab sent zuordnen (siehe 9a)
8. Stornieren (über den Status nur bei Brutto ≤ 0): Stornorechnung als eigener Beleg, Gegenbuchung der Forderung (siehe 9b)
9. Erstatten (Guthaben nach dem Storno): ausgehende Bankbewegung an der Stornorechnung zuordnen (siehe 9b)
```

### AfA-Berechnung

```
1. AssetCategories → Nutzungsdauer + Methode
2. Assets → Erfassung
3. AssetDepreciations → AfA-Plan: Formel (calculate_depreciation_plan) und Fortschreibung
   nach gebuchten Jahren (depreciation_plan: gebuchte Jahre bleiben, der Rest verteilt sich neu)
4. Buchungsintegration über Booking_ID: book_depreciation legt je Jahr genau eine
   Umbuchung „Aufwand an Anlagekonto“ an (Betrag positiv, ohne Account_ID, 31.12.),
   Sperre gegen Doppelbuchung in derselben Transaktion
5. EÜR: Section 3 (Aufwandskonto = −AfA); DATEV-Export: „Aufwand an Anlagekonto“, BU 0
```

Das Anlagekonto kommt aus dem Formular, sonst aus der Anlage oder ihrer Kategorie. Bei der
EÜR wird direkt abgeschrieben (Gegenkonto = Anlagekonto, kein Wertberichtigungskonto).
Aufwandskonto = Erfolgskonto (4000–7999) und Anlagekonto = Konto 1–999 werden geprüft, damit
kein Satz wie „6220 an 1460“ entsteht; nach dem Abgang der Anlage (`SaleDate`) wird keine AfA
mehr gebucht – der Plan endet mit dem Abgangsjahr. Die zeitanteilige AfA im Abgangsjahr plant die
Anwendung noch nicht (volle Jahres-AfA, `docs/aufgaben.md`). Erweiterungen (Unter-Anlagen) erhöhen den
Anschaffungswert der Hauptanlage und werden mit ihr abgeschrieben (siehe „AfA-Plan der App“).
Hinweis GWG: Wer den Kauf schon auf das Aufwandskonto 6260 (Sofortabschreibung) gebucht hat
und zusätzlich die AfA bucht, zählt doppelt – das Programm prüft das nicht.

### DATEV-Export (seit 2.3.0, `export/datev.py`, Regeln in `db/datev_keys.py`)

Der Stapel (EXTF 700) bildet den nach, den WISO Mein Büro schreibt – gemessen an einem Datenbestand; ob die Kanzlei ihn so annimmt, klärt das
Steuerbüro. Das Soll/Haben-Kennzeichen folgt der Form des Satzes. Der **BU-Schlüssel** (Spalte 8):

```
1. Bookings.DatevKey gesetzt (Import, 1–9999)  → unverändert ausgeben (auch 490, 100, 121 ohne Satz)
2. sonst ableiten nach Art des Zweckkontos und Satz:
     Erlöskonto (4000–4999 und 6884–6892) → Umsatzsteuer 101 (19 %) / 102 (7 %) / 5 (16 %) / 4 (5 %)
     anderes Zweckkonto → Vorsteuer 401 / 402 / 7 / 6, auch bei einer Erstattung
     Debitor, Spiegel, kein Satz, anderer Satz → 0   (auch die AfA „Aufwand an Anlagekonto“: Vorsteuer-Art, aber ohne Satz)
   vom 2020-07-01 bis 2020-12-31 tauschen die Plätze (16 %/5 % = Regelsatz/ermäßigt, 19 %/7 % = die anderen)
3. Rechnungskette (Umbuchung mit Debitor oder Wartekonto: Forderung, Wartekonto an Erlöskonto, Stornogegenbuchung)
   → Schlüssel 0, aber der Satz in Spalte 40 (EU-Steuersatz, „19,00“)
```

Die Konten 6884–6892 („Erlöse aus Verkäufen … (bei Buchverlust)“, z. B. 6885 mit 19 % USt) liegen im Aufwandsbereich, zählen für den Schlüssel
aber als Erlöskonten (`DISPOSAL_REVENUE_RANGE`).

Dazu: Leistungsdatum (114) nur an der Forderung, Land (119) an der Rechnungskette, Spalte 115 leer, Belegfeld 1 ohne Leerzeichen bis 36
Zeichen, Buchungstext ohne Zeilenumbrüche und andere Steuerzeichen (je Zeichen ein Leerzeichen, vor dem Kürzen auf 60 Zeichen), im Vorlauf
Berater- und Mandantennummer der eigenen Firma (`CompanyDetails`, 1001–9999999 bzw. 1–99999 ohne führende Null, sonst Platzhalter). Die Rechnungsdaten (Leistungsdatum,
Land) liest der Handler in einer Abfrage über alle Rechnungen (`get_datev_invoice_data`), die Wartekonten aus `OPEN_REVENUE_ACCOUNTS`
(`get_waiting_account_numbers`).

**Zeitraum und Vollständigkeit des Stapels (seit 2.3.4, `handle_datev_export`):** Der Stapel kennt vom Belegdatum nur Tag und Monat und beginnt sein
Wirtschaftsjahr am 1. Januar des Jahres von „von“ – ein Zeitraum über den Jahreswechsel wird abgelehnt (je Kalenderjahr einzeln exportieren). Reihenfolge der
Prüfungen: Datum fehlt, ungültig, „von“ nach „bis“, Jahreswechsel, unstimmige Splits, Hinweise, keine Buchungen. Gerechnet wird nicht mit Paaren, sondern mit
Gruppen (`db.matching.settle_transfers`, reine Funktion; `Database.check_transfers` lädt den ganzen Bestand und ruft sie auf, höchstens O(n log n): Gruppen über
Wörterbücher, je Gruppe einmal sortiert, das Umfeld über Halbierungssuche):

- **Beine.** Eigene Konten sind die Bank-/Kassenkonten mit SKR-Nummer; tragen zwei Konten dieselbe Nummer, nimmt keins von beiden teil. Ein *Bein mit Spiegel*
  ist eine Bankbewegung R auf einem eigenen Konto X mit einem Kind, das ein Spiegel (`is_mirror_row`) genau zwischen X und einem anderen eigenen Konto Y ist
  (Betrag des Beins = Betrag des Spiegels, ein Teilspiegel im Split zählt mit seinem Betrag); ein *Bein ohne Satz* ist eine Bankbewegung ohne ein einziges Kind.
  Die Richtung kommt allein aus dem Vorzeichen von R (R < 0: Ausgang X → Y, R > 0: Eingang), nie aus der Form des Satzes – ein `mirror_legacy` wird nicht
  gedeutet. Ein *eigenständiger Spiegel* (ohne Bankbewegung) nimmt nur mit bekannter Lesart (`mirror_coa`/`mirror_counter`, Richtung über `bank_effect`) an den Gruppen teil;
  mit `mirror_legacy` oder ohne Form legt er nichts zusammen und deckt nichts (`known=False`), kann aber im dritten Hinweis stehen. Eine Bankbewegung mit Kindern,
  aber ohne Spiegel-Kind ist *anders kontiert* (`TransferOther`: Zahlung, Umbuchung in anderer Form): Sie bildet keine Gruppe und legt nichts zusammen, zählt in der Deckung
  aber als verbraucht und lässt die Gegenseite im dritten Hinweis nennen. **Nur ein ISO-Datum und eine ganze Zahl nehmen teil:** das Datum genau `JJJJ-MM-TT` (ein
  Kalendertag, Jahr 1900–2200 – dieselbe Regel wie `server.period.parse_iso_date`; `20260305`, `2026-W10-4`, ein Zeitanteil, `0001-01-03`, `9999-12-30`, NULL oder eine Zahl
  nicht), der Betrag ein `int` (kein Text, keine Kommazahl, kein `bool`). Eine Bankbewegung damit nimmt gar nicht teil; ein Satz damit ist kein Bein und kein Spiegel,
  seine Bankbewegung gilt als anders kontiert. So läuft das Umfeld (± Tage) nie über das Datumsende, und ein Satz, den ein Import mit anderer Schreibweise ablegte, legt
  nichts zusammen, was der Stapel dann nicht trägt.
- **Gruppen, Abschnitte, Umfeld.** Je Ausgangskonto, Eingangskonto und Betrag eine Gruppe (die Beine ohne Satz gehören zu jeder Gruppe ihres Kontos und Betrags,
  die schon durch ein Bein mit Spiegel oder einen eigenständigen Spiegel besteht); je Gruppe nach Datum sortiert und bei einer Lücke von mehr als
  `TRANSFER_DAY_WINDOW` (7) Tagen in Abschnitte zerlegt. Das *Umfeld* eines Abschnitts sind alle Elemente der Gruppe, die höchstens `TRANSFER_NEIGHBOUR_DAYS` (14)
  Tage vor seinem ersten oder nach seinem letzten Element liegen (er selbst gehört dazu), dazu die anders kontierten Ausgänge auf X und Eingänge auf Y mit diesem Betrag.
  Je Abschnitt: a_m/b_m Ausgänge/Eingänge mit Spiegel, a_c/b_c ohne Satz, s eigenständige Spiegel; im Umfeld A/B Ausgänge/Eingänge mit Spiegel, a_o/b_o anders kontierte.
- **Zusammenlegen** (`Database.check_transfers`, `omitted`; `duplicate_transfer_rows`): Die Eingangs-Sätze eines Abschnitts fallen aus dem Stapel genau dann, wenn
  a_m ≥ 1, b_m ≥ 1 und a_m ≥ b_m + b_c gilt und im Umfeld kein Ausgang ohne Satz und kein eigenständiger Spiegel liegt – jeder Eingang gehört dann zu einem Ausgang,
  dessen Satz im Stapel steht, und kein Eingangs-Satz ist der einzige seines Übertrags (dessen Ausgang läge sonst, ohne Satz, im Umfeld); es fallen alle b_m weg oder keiner
  (zugeordnet wird nicht). Anders kontierte Bewegungen ändern daran nichts. Die Regel gilt über den ganzen Bestand, nicht je Zeitraum; die weggelassenen Sätze bekommen kein
  Steuerdatum. Ein Übertrag über 1460 (zwei echte Teilbuchungen) wird nie zusammengelegt.
- **Möglicherweise doppelt** (dritter Hinweis): Ein Eingangs-Satz im Zeitraum, der bleibt, obwohl im Umfeld seines Abschnitts ein Ausgang mit Spiegel oder ein anders
  kontierter Ausgang liegt, steht möglicherweise doppelt im Stapel; ebenso ein Ausgangs-Satz, in dessen Umfeld ein anders kontierter Eingang liegt (a_m ≥ 1 und b_o ≥ 1, auch
  in einem zusammengelegten Abschnitt – dessen Eingangs-Satz fällt dann trotzdem weg); ebenso ein eigenständiger Spiegel im Zeitraum, in dessen Umfeld ein Bein mit Spiegel
  liegt (ein Import-Spiegel und eine von Hand kontierte Bankbewegung), und ein eigenständiger Spiegel ohne bekannte Lesart (`mirror_legacy`, keine Form), neben dem im Umfeld
  ein Bein mit Spiegel zwischen denselben beiden Konten und mit demselben Betrag liegt – genannt, nicht gedeutet und nie weggelassen. Ein weggelassener Satz steht nie in
  dieser Liste. Die Meldung nennt dazu den **Auslöser im Umfeld** (` (Auslöser im Umfeld: Datum Betrag EUR)`, nur Datum und Betrag): die älteste Bewegung, die das Zusammenlegen
  sperrt (Ausgang ohne Satz, eigenständiger Spiegel) oder den Hinweis auslöst (anders kontierte Bewegung) – bei einer langen Reihe zeigt er die Lücke, die sie ganz sperrt. Er fehlt,
  wo nichts sperrt (zwei Abschnitte mit einem Ausgang mit Spiegel im Umfeld, eigenständiger Spiegel).
- **Bankbewegungen ohne Buchungssatz** (`Database.find_unbooked_bank_movements`, erster Hinweis): Eine Bankzeile im Zeitraum mit Betrag ≠ 0 (eine Zahl) und ohne ein einziges Kind fehlt im Stapel
  (er enthält nur Sätze). Gedeckt – und nicht gemeldet – ist sie, wenn in einem Abschnitt ihrer Gruppe die überschüssigen Spiegel der Gegenseite für alle
  Bewegungen ohne Satz ihrer Seite reichen: Ausgänge ohne Satz a_c ≤ max(0, b_m − A − a_o) + s, Eingänge ohne Satz b_c ≤ max(0, a_m − B − b_o) + s, wobei A und B die Ausgänge und
  Eingänge mit Spiegel und a_o, b_o die anders kontierten im UMFELD des Abschnitts sind (er selbst gehört dazu): Ein Eingangs-Satz, dessen Ausgang mit Satz oder anders kontiert in
  der Nähe steht, ist dessen Gegenseite und deckt keinen anderen Ausgang. Ein eigenständiger Spiegel zählt als s nur, wenn im Umfeld kein Bein mit Spiegel liegt (sonst steht er im
  dritten Hinweis); anders kontierte Bewegungen zehren s nicht auf. Reicht es nicht, ist keine dieser Seite im Abschnitt gedeckt (alles oder nichts, je Abschnitt). So wird ein ganz
  unkontierter Übertrag zwischen kontierten gemeldet. Konten ohne SKR-Nummer oder mit doppelter Nummer sind nie gedeckt.
- **Überträge über den Zeitraum hinaus** (zweiter Hinweis), **je Abschnitt gezählt, nicht je Satz** (`_boundary_entries`). Stehen im Zeitraum mehr weggelassene Eingangs-Sätze eines
  Abschnitts als Ausgangs-Sätze dieses Abschnitts, fehlen die überzähligen in diesem Stapel – ihr Übertrag steht mit seiner Ausgangsseite im Stapel eines anderen Zeitraums; der Export
  sagt es und nennt genau so viele Sätze, wie überzählig sind: „… steht mit seiner Ausgangsseite außerhalb des Zeitraums und deshalb nicht in diesem Stapel“. Eine Reihe gleichzeitig
  gebuchter Überträge (ein Dauerauftrag über 100 Wochen, monatlich exportiert) braucht damit keinen Hinweis; nur Überträge, die eine Zeitraumgrenze wirklich überqueren, werden genannt.
  Gemeint sind die Sätze am Rand, über den der Übertrag läuft: vom Anfang so viele, wie zu Beginn des Zeitraums unterwegs sind (Ausgänge vor dem Zeitraum, deren Eingang noch aussteht),
  der Rest vom Ende. Umgekehrt (zweiter Satz derselben Meldung): Stehen im Zeitraum mehr Ausgangs-Sätze eines Abschnitts als weggelassene Eingangs-Sätze und fallen Eingangs-Sätze dieses
  Abschnitts außerhalb weg, stehen die überzähligen Ausgangs-Sätze (höchstens so viele, wie außerhalb wegfallen) hier, ihre Eingangsseite in einem anderen Stapel – „wurde der andere
  Zeitraum schon exportiert, bevor beide Seiten kontiert waren, steht er dort ebenfalls“. Beide Sätze stehen in einer Meldung, der erste zuerst.
- **Bestätigung.** Gibt es Hinweise (null bis drei, in dieser Reihenfolge: Bewegungen ohne Satz mit Anzahl, Eingängen und Ausgängen getrennt und den ersten fünf; Überträge über den
  Zeitraum hinaus; Überträge, die möglicherweise doppelt stehen, mit den ersten fünf und dem Auslöser), exportiert der Handler nur mit `confirm_export` = `1` und `confirmed_check` = der
  Prüfsumme (SHA-256, 16 Zeichen) über den Zeitraum und **alle Einträge aller Hinweise** – je Hinweisart ID, Datum und Betrag in Minor Units, dazu der Auslöser –, **nicht über den
  Wortlaut** (der nennt nur die ersten fünf: Wird eine Bewegung kontiert und kommt eine andere dazu, gilt die alte Bestätigung nicht mehr); sonst leitet er mit `datev_export=confirm` auf
  *Sonstiges* zurück (Pflicht-Kästchen). Ändert sich Zeitraum oder Bestand, passt die Prüfsumme nicht mehr, und der Hinweis kommt neu. Die Meldungen nennen nie Text oder Empfänger. Die Seite
  nennt die Regel für Überträge dauerhaft im DATEV-Abschnitt (der Export liefert eine Datei, die kein Skript der Seite sieht).
- **Grenzen der Zählregel** (belegt durch `tests/test_datev_transfer_enumeration.py`, das alle kleinen Bestände durchspielt – vollständige Kontoauszüge vorausgesetzt). Bis zu 14 Tagen
  Verzögerung zwischen den beiden Seiten eines Übertrags geht kein Übertrag verloren, und jede Doppelung zwischen Spiegel-Sätzen (auch neben einer anders kontierten Seite) wird genannt.
  Darüber hinaus bleibt die Regel stehen: Ein Übertrag kann doppelt im Stapel stehen, ohne dass der Export es sagt, und – selten – in einem Zeitraum fehlen (meist meldet der erste Hinweis dann
  die Bewegung ohne Satz). Die Deckung rechnet je Abschnitt: Liegen die beiden Seiten eines Übertrags weiter als das Fenster auseinander, meldet der erste Hinweis die satzlose Seite, obwohl
  der Übertrag im Stapel steht (ein Falschalarm). Ungemeldet bleibt ein ganz unkontierter Übertrag zwischen zwei halb kontierten Überträgen gleichen Betrags, deren kontierte Seite
  jenseits des Fensters von der satzlosen liegt (mit drei Überträgen gleicher Höhe in wenigen Wochen; deren satzlose Seiten werden dann gemeldet), und – eine Ecke der anders kontierten
  Seiten – neben einem Übertrag, dessen beide Bankbewegungen anders kontiert sind und zu dem ein eigenständiger Spiegel steht (drei Erfassungen desselben Übertrags). Im Zweifel bleibt jede
  Zeile stehen. Weitere bekannte Grenzen, jede mit einem benannten Test in `tests/test_datev_transfer_hints.py` (`test_known_limit_…`):
  - *Sammelüberweisung mit anderer Stückelung*: A −200 mit einem Spiegel über 200, auf B zwei Gutschriften über je 100 mit je einem Spiegel (oder −100 gegen zweimal 50, dazu eine
    Lieferantenzahlung über 50): Die Beträge unterscheiden sich, also sind es verschiedene Gruppen – der Stapel trägt die Buchung doppelt, und niemand sagt es.
  - *Zwei eigene Konten mit derselben SKR-Nummer*: Keins von beiden nimmt teil; ein beidseitig kontierter Übertrag zwischen einem von ihnen und einem anderen Konto steht mit beiden Sätzen im
    Stapel, ohne Hinweis. (Ein Paar zweier Spiegel-Kinder mit gleichem Betrag binnen sieben Tagen zu nennen wäre eine Paarbildung außerhalb der Zählregel.)
  - *Eine weit entfernte Lücke sperrt eine ganze Reihe*: Fehlt in einem lückenlosen Dauerauftrag (alle Abstände höchstens sieben Tage) irgendwo, auch Monate davor, der Satz einer Seite, ist die
    Reihe ein einziger Abschnitt, und der Ausgang ohne Satz sperrt ihn ganz – nichts fällt weg, die Eingangs-Sätze stehen doppelt und werden genannt (dritter Hinweis); der Auslöser im Umfeld
    nennt die Lücke.
  - *Ein eigenständiger Spiegel neben einer anders kontierten Bankbewegung* wird nicht genannt (nur neben einem Bein mit Spiegel).
  - *Ein Text als Betrag auf einer Bankbewegung mit Sätzen im Zeitraum* lässt die Prüfung der Splits (`find_unbalanced_splits`, vor den Hinweisen) mit einer Fehlermeldung scheitern – dieser Weg
    ist nicht Teil der Zählregel und nicht geändert.

---

## Besonderheiten

1. **Foreign Keys:** `PRAGMA foreign_keys = ON`
2. **ON DELETE CASCADE:** InvoiceItems, AssetDepreciations, ContactAddresses, CompanyDetails, PersonDetails, DocumentReadings
   (nicht: `InvoiceCorrections`, `InvoiceDocumentVersions` – Protokoll und Fassungen überleben das Löschen eines Entwurfs).
   `WorkTimes.PersonID` und `Trips.DriverID` tragen im Schema ebenfalls CASCADE, `WorkTimes.CustomerID` und `Trips.DocumentID`
   SET NULL – die App lässt es dazu nicht kommen: Kontakt, Artikel, Bankkonto, AfA-Kategorie und Beleg werden nur gelöscht,
   solange nichts auf sie verweist (`db.DeleteRefused`, seit 2.3.8); die Lesung eines Belegs (`DocumentReadings`, abgeleitete Daten) ist kein
   solcher Verweis – sie geht mit dem Beleg
3. **Snapshot-Prinzip:** Invoices kopieren Stammdaten bei Erstellung
4. **Datum-Format:** ISO 8601 (YYYY-MM-DD)
5. **Steuersätze:** Dezimal (0.19 = 19%)
6. **Seeding:** `_seed_chart_of_accounts()`, `_seed_asset_categories()`, `_seed_tax_keys()` laden aus `seed_data/`-JSON
   (nur in leere Tabellen; fehlende Standardkonten eines bestehenden Bestands: `missing_standard_accounts()` /
   `add_missing_standard_accounts()`, nur auf Knopfdruck)
7. **Geldbeträge:** ganze Zahlen (`INTEGER`) in Minor Units, 1 = 0,0001 € (`money.py`, kaufmännisch gerundet); Steuersätze, Mengen und Prozentwerte sind `REAL`

---

**Letzte Aktualisierung:** 21. November 2026 (Schema 2006)
