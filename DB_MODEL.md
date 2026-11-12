# Datenbankmodell uBuchhaltung

**Status:** Aktuell (Stand: 7. Juli 2026)
**DBMS:** SQLite 3
**Datei:** `data/users/<Name>/buch.db`

---

## Übersicht

uBuchhaltung verwaltet Buchführung für kleine/mittlere Unternehmen mit Fokus auf deutsche Buchhaltungsstandards (SKR03/SKR04). Es unterstützt XRechnung-konforme Rechnungserstellung, Anlagenverwaltung (AfA), automatische Bank↔Entry-Verknüpfung und Belegmanagement.

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

Beim Erstellen der Datenbank werden folgende Tabellen automatisch befüllt (nur wenn leer):

| Tabelle | Quelle | Einträge |
|---------|--------|----------|
| ChartOfAccounts | `seed_data/chart_of_accounts_skr04.json` + optional `seed_data/private/` | 56 Standard + eigene |
| AssetCategories | `seed_data/asset_categories.json` | 30 BMF-Kategorien |
| TaxKeys | `seed_data/tax_keys.json` | 50 DATEV-BU-Schlüssel |

### Testdaten (`seed_data/test/`)

Nur über `load_test_seed_data()` geladen (z.B. bei `--test-data`-Flag). Idempotent (kein Doppel-Einfügen):

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
| PrivateSharePercent | INTEGER | DEFAULT 0 | Privatanteil in % (0–100); wird bei Matching/Export berücksichtigt |
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

**Business Logic:**
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
| Amount | 11 | REAL | NOT NULL | Betrag (+Haben, −Soll) |
| Currency | 12 | TEXT | DEFAULT 'EUR' | Währung |
| TaxRate | 13 | REAL | | Steuersatz (0.19 = 19%) |
| TaxAmount | 14 | REAL | | Berechneter Steuerbetrag |
| Text | 15 | TEXT | | Verwendungszweck |
| DocumentNumber | 16 | TEXT | | Belegnummer |
| BookingType | 17 | TEXT | DEFAULT 'entry' | **'bank'** = Bankbewegung, **'entry'** = Buchungssatz |
| ParentBooking_ID | 18 | INTEGER | FK → Bookings(ID) | Verknüpfung: Entry → Bank-Buchung |
| Status | 19 | TEXT | | Frei definierbar; belegt sind `'resolved'` (Debitoren-Entry aufgelöst), `'posted'` (AfA-Buchung) und `'invoice_payment'` (Satz einer Rechnungszahlung, siehe 9a) |
| AutoMirror | 20 | INTEGER | DEFAULT 0 | 1 = die App hat den Satz selbst als Spiegel der Bankbewegung angelegt (wird beim Bearbeiten der Bankzeile mitgezogen) |
| SourceGroup | 21 | TEXT | | Klammer aus dem Quellsystem (WISO `ACCOUNTINGID`) |
| EntryForm | 22 | TEXT | | Form des Buchungssatzes (siehe unten): `purpose_first`, `liquid_first`, `transfer`, `mirror_coa`, `mirror_counter`, `mirror_legacy`. Bankzeilen haben keine (NULL). **Immer am Ende angefügt** – die Leser lesen `Bookings` positionsbasiert (`SELECT *`) |

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
bei Zahlung ertragswirksam – so sieht das Steuerbüro im DATEV-Export die noch
offenen Posten (Sollversteuerung). Das entspricht dem Verfahren von WISO Mein
Büro und damit auch der Struktur importierter Altdaten.

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
Marke `Status='invoice_payment'`. Es gibt eine Stelle, die Zuordnungen anlegt
(`assign_invoice_payment`, `db/invoice_payments.py`), und eine, die sie entfernt
(`unassign_invoice_payment`).

- **Voraussetzungen:** nur Zahlungseingänge (Bankbewegung mit Betrag > 0) auf versendete Rechnungen
  (`sent`, `overdue`, `partial_payment`; `PAYABLE_STATUSES`) mit positivem Brutto; das Bankkonto der
  Bewegung braucht eine SKR-Nummer; das Paar (Rechnung, Bewegung) gibt es höchstens einmal. Negative
  Zuordnungen (Storno, Gutschrift) kennt nur der WISO-Import (`link_invoice_to_transaction`, mit
  Vorzeichen und ohne eigene Sätze). Einer **Stornorechnung** (`InvoiceKind='storno'`) lässt sich nie eine
  Zahlung zuordnen (`payment_blocker`; auch `link_invoice_to_transaction` lehnt ab), und sie ist nie ein
  Kandidat für die Zuordnung.
- **Betrag:** min(Rest der Rechnung, freier Rest der Bewegung, gewünschter Betrag); der gewünschte Betrag
  wird vorher auf Cent gerundet (halb auf, `118,995 → 119,00`; ≤ 0 nach dem Runden lehnt ab), sonst bliebe
  ein Bruchteil eines Cents auf dem Debitorenkonto stehen. Frei ist
  `Betrag − MAX(Σ Zuordnungen, Σ bankwirksame Sätze darunter)` (`_free_minor_sql`, dieselbe Formel für
  Zuordnung, Kandidatenlisten und Kontakt-Guthaben): eine schon als Erlös kontierte Bewegung ist keine
  Rechnungszahlung mehr. *Sammelzahlung* = mehrere Zuordnungen derselben Bewegung, jede mit eigenen
  Sätzen; *Teilzahlung* = mehrere Bewegungen auf eine Rechnung; bei einer *Überzahlung* bleibt der Rest
  frei (Guthaben beim Kontakt; der DATEV-Export bleibt gesperrt, bis er kontiert ist).
- **Erlöskonto:** Es ergibt sich aus der Rechnung (`resolve_revenue_coa`: gelernt, sonst 4400 bei
  19 %, 4300 bei 7 %, 4185 steuerfrei/§ 19). **Gelernt wird nur aus den markierten Sätzen** der
  Zuordnungen (`Status='invoice_payment'`) von Rechnungen mit demselben Steuersatz, und nur in der Form,
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
  zwei gleichzeitige Anfragen legen das Paar nicht doppelt an (es gibt keinen UNIQUE-Index). Konten,
  die dabei angelegt werden könnten (`resolve_revenue_coa`), löst die Funktion vorher in eigenen
  Verbindungen auf.
- **Entfernen:** `unassign_invoice_payment` löscht die markierten Sätze des Paars (nie Sätze mit
  `SourceGroup`, nie Sätze ohne Marke – Altbestand und WISO-Sätze bleiben stehen) und die Zuordnung,
  setzt eine durch Stufe 7 aufgelöste Forderung wieder auf offen, wenn die übrigen Sätze „… an Debitoren“
  sie nicht mehr ausgleichen (Summe < Forderungsbetrag, `money.is_covered`), und rechnet die Rechnung
  neu. War es die letzte Zahlung und ist der Status der Rechnung kein Forderungsstatus
  (`RECEIVABLE_STATUSES`: versendet, Teilzahlung, überfällig, bezahlt – also etwa storniert), geht auch
  die Forderung (die die App selbst gebucht hat, nie ein Satz mit `SourceGroup`), wie beim Statuswechsel:
  „erst von Hand stornieren, dann die Zahlung entfernen“ endet im selben Zustand wie die umgekehrte Reihenfolge –
  nicht aber bei einer Rechnung mit Stornorechnung: Ihre Forderung und die Gegenbuchung bleiben stehen. `delete_transaction` einer Bankbewegung nimmt ihre Sätze und
  Zuordnungen mit; das Löschen eines markierten Satzes (auch im Split-Editor) entfernt die ganze
  Zuordnung.
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
  (`money.is_covered`) – nicht schon nach der ersten Teilzahlung.
- **Prüfung:** `find_payment_inconsistencies(invoice_id=None)` meldet, rein lesend, Zuordnungen ohne Satz
  „Bank an Debitoren“, Sätze „Bank an Debitoren“ ohne Forderung (10000 im Haben-Überhang), an einer
  Bankbewegung hängende Forderungen, Zahlungssätze ohne Zuordnung (die markierten Sätze der App und
  bankwirksame Erlössätze unter einer Bewegung, deren Beleg-Nr. eine Rechnungsnummer ist, ohne Zuordnung
  für das Paar – unabhängig vom Text), Zuordnung ≠ Umbuchung, Zuordnungen auf einen Satz oder einen
  Ausgang, doppelte Zuordnungen, versendete Rechnungen mit Status „Entwurf/abgeschlossen“ und einen Rest,
  der nicht zu den Zuordnungen passt (der Rest einer Stornorechnung ist ihr negativer Betrag: kein Befund). Zuordnungen, deren Buchung keine Bankbewegung ist oder deren Sätze
  eine `SourceGroup` tragen (WISO liefert seine Sätze selbst, z. B. Barzahlungen ohne Bankbewegung),
  lösen weder „Zuordnung ohne Ausgleichssatz“ noch „Zuordnung ≠ Umbuchung“ aus. Die Abfragen gehen von
  den Zuordnungen aus bzw. fassen je Rechnung vorab zusammen: Der Aufwand wächst linear mit dem Bestand
  (4000 Bewegungen, 4000 Forderungen, 1000 Zuordnungen: unter einer Sekunde). Die Liste steht auf der
  Seite *Verschiedenes* und läuft **nur auf Klick** („Zahlungszuordnungen prüfen“, `GET
  /miscellaneous?check_payments=1`), nicht bei jedem Laden der Seite; der Hinweis auf der Rechnungsseite
  prüft nur diese eine Rechnung.

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
  gezahlt, steht das Guthaben des Kunden als Haben-Saldo auf den Debitoren, bis es erstattet wird (offen,
  `docs/aufgaben.md`). Der Stornorechnung lässt sich keine Zahlung zuordnen.
- **Gesperrt:** Die Stornorechnung ist ganz gesperrt (`db.invoice_lock.row_group`): Status, Speichern, Berichtigen,
  Löschen, Kopieren werden abgelehnt. Eine stornierte Rechnung mit Stornorechnung lässt sich nicht
  wiederbeleben.
- **Dokument:** CII Typ 381 mit `ram:InvoiceReferencedDocument` (BT-25/BT-26), UBL als `CreditNote` mit
  `cac:BillingReference`; im XML stehen die Beträge positiv, das PDF zeigt die gespeicherten negativen.

Alles läuft in einer Transaktion (`BEGIN IMMEDIATE`): Es gelingt ganz oder gar nicht, eine Nummer wird nur bei
Erfolg verbraucht. Zeigt dabei ein Verweis der Rechnung ins Leere (gelöschtes Bankkonto, Kunde …), nennt die Antwort ihn
im Klartext (409); repariert wird nichts.

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
| NumberFormat | TEXT | DEFAULT '{yy}{l}{nnn}{s}' | Format-Template, z.B. `{yy}{l}{nnn}{s}` → `26F001` |

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
| ReplacesInvoiceId | 52 | INTEGER | | neue Rechnung: die ersetzte, stornierte – noch ungenutzt |
| SentAt | 53 | DATETIME | | erstmals versendet (UTC, gesetzt beim Übergang `send`); Import und Altbestand: NULL |
| DocumentVersion | 54 | INTEGER | | Nummer der aktuellen Fassung; NULL = Fassung 1 |

Die Spalten 50–54 hängen an der Tabelle: frische und migrierte Datenbank haben dieselbe Reihenfolge
(`db.invoices.InvoiceCol`). Der Bestand behält überall NULL.

**Fassungen und Berichtigen:** Eine versendete Rechnung hat gesperrte Finanzangaben (`db/invoice_lock.py`);
Zahlungshinweis, Anschrift, Bestellnummer, Leitweg-ID und Leistungsdatum ändert nur eine **Berichtigung**
(`Database.correct_invoice`, `db/invoice_versions.py`): Die Dateien der bisherigen Fassung wandern nach
`invoices/<Jahr>/Fassungen/<ID>/` (Tabelle 13a), jedes geänderte Feld steht im Protokoll (13b), und
`DocumentVersion` zählt um eins hoch. Zurückholen auf Entwurf legt die Dateien ebenso ab (und zählt hoch, wenn
eine Datei abgelegt wurde). Die berichtigte Fassung trägt einen Hinweis (PDF-Zeile, CII BT-22, UBL `cbc:Note`),
der sich aus dem Stand dieser Tabellen ergibt (`Database.correction_notice`).

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
| InvoiceID | INTEGER | NOT NULL, FK → Invoices (ON DELETE CASCADE) | |
| BookingID | INTEGER | NOT NULL, FK → Bookings | Bankbewegung der Zahlung (Altbestand: auch ein Buchungssatz) |
| Amount | INTEGER | NOT NULL | Zahlungsbetrag in Minor Units (1 = 0,0001 EUR). Das Vorzeichen trägt nur der WISO-Import (Storno, Gutschrift); die Oberfläche legt nur positive Beträge an |
| PaymentDate | DATE | NOT NULL | Datum der Bankbewegung |
| Notes | TEXT | | |
| CreatedAt | DATETIME | | |

Das Paar (`InvoiceID`, `BookingID`) kommt höchstens einmal vor. Jeder Schreiber
(`assign_invoice_payment`, `link_invoice_to_transaction`) prüft und fügt in einer
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
| Status | TEXT | DEFAULT 'active' | 'active', 'sold', 'scrapped' |
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
| Booking_ID | INTEGER | FK → Bookings | AfA-Buchung (siehe „AfA-Buchung“ bei Bookings) |
| Status | TEXT | DEFAULT 'planned' | 'planned', 'posted' (vom Programm gebucht), 'booked' (WISO-Import, ohne Booking_ID) |
| BookedAt | DATETIME | | |

**Indizes:** UNIQUE(Asset_ID, Year)

**Business Logic:** Je Anlage und Jahr gibt es höchstens eine AfA-Buchung: `book_depreciation`
lehnt ein Jahr ab, dessen Buchung es gibt (`Booking_ID` zeigt auf eine vorhandene Zeile in
`Bookings`) oder das den Status `booked` hat (`ValueError`, Prüfung und Anlegen in einer
Transaktion). „Gebucht“ heißt also: die Buchung gibt es. Eine Zeile `posted`, deren Buchung
fehlt (z. B. per SQL-Konsole gelöscht), gilt als nicht gebucht – die Anlagenseite bietet das
Buchen wieder an und `book_depreciation` überschreibt die Zeile. Wird die Buchung über die
Anwendung gelöscht (`delete_transaction`, ebenso die SQL-Vorlage „Zeitbereich löschen“), geht
die Zeile auf `planned` zurück. `delete_asset` lehnt ab, solange eine AfA-Buchung existiert
oder Unteranlagen existieren; `booked`-Zeilen aus dem WISO-Import (ohne Buchung) hindern das
Löschen nicht.

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
- **Liquide Konten** (`get_payment_coa_ids`): eigene Bank-/Kassenkonten (die Standard-Kasse trägt 1600) und das Verrechnungskonto 1460 – nur noch für Zeilen ohne gespeicherte Form (Roh-SQL), die die EÜR damit einordnet. Ob ein Satz zählt, entscheidet die Sektion in `get_euer_data()`. Jeder Satz zählt in genau einer Sektion: Die Sammlung ist über die Buchungs-ID eindeutig, die erste Sektion, die einen Satz findet, gewinnt (Reihenfolge 1, 2, 2b, 3); außerdem sind die Sektionen so geschnitten, dass sich die üblichen Fälle gar nicht erst überschneiden:
  - **Section 1:** Buchungssätze (Kinder) unter einer Bankbewegung der gewählten Konten – auch der Kasse (Buchungsmaske mit Konto = Kasse; die Bankzeile trägt `Account_ID` der Kasse)
  - **Section 2:** eigenständige Sätze (ohne `ParentBooking_ID`), die ein Kassenkonto berühren, und – nur ohne Kontofilter – das Verrechnungskonto 1460 (WISO: „Aufwand an Verrechnung“ gilt mit dem Datum des Satzes als abgeflossen; die spätere Übertragung „Verrechnung an Bank“ ist ein Geldkonten-Spiegel und zählt nicht noch einmal)
  - **Section 2b (Kasse):** eigenständige Sätze mit `Account_ID` auf einer Kasse (`IsCash=1`), die in Section 2 nicht vorkommen (z. B. `CounterCOA_ID IS NULL`; ältere Einträge, Kasse ohne SKR-Nummer). Sie laufen durch dieselbe Lesart der Form wie alle Sätze (`read_entry_form`). AfA-Buchungen alter Form und Umbuchungen zwischen Anlage- und Erfolgskonto gehören nicht dazu, auch wenn die Maske ihnen eine Kasse als Konto eingetragen hat – sie zählen in Section 3 und mit einem Kontofilter „nur Kasse“ gar nicht (AfA hängt an keinem Konto). Section 2b wird in `get_euer_data()` und `get_dashboard_monthly/totals()` berücksichtigt.
  - **Section 3 (nicht zahlungswirksam, nur ohne Kontofilter):** eigenständige Umbuchungen zwischen Anlagekonto (SKR04 1–999) und Erfolgskonto (4000–7999) – AfA „Aufwand an Anlagekonto“, Restbuchwert beim Abgang (`is_asset_transfer`) – und die Sätze, auf die `AssetDepreciations.Booking_ID` zeigt (auch alte AfA-Sätze). Das Anlagekonto selbst wird bei Umbuchungen nicht bebucht. Solche Sätze tragen keine Zahlung: `TaxAmount`/`TaxRate` bleiben unberücksichtigt (keine Vorsteuer auf 1401/1406). Der Anlagenkauf mit Vorsteuer als Kind einer Bankbewegung (Section 1) ist davon nicht berührt.
  - Das **Dashboard** (Zahlungskarten) folgt den Kontobewegungen: Bankbewegungen aller gewählten Konten inkl. Kasse sowie eigenständige Sätze auf Kassenkonten (Section 2/2b); das Verrechnungskonto 1460 und die AfA zählen dort nicht. Ein eigenständiger Spiegel mit bekannter Lesart (`mirror_coa`, `mirror_counter`) zählt aus Sicht der Kasse (Abhebung +, Bareinzahlung −); jede andere Zeile mit ihrem Betrag. Dass ein Übertrag Bank ↔ Kasse dabei als Einnahme bzw. Ausgabe zählt, ist ein offener Punkt (`docs/aufgaben.md`).
- **Bestandskonten** (`NEUTRAL_ACCOUNT_NUMBERS`: 10000 Debitoren, Wartekonten 4340/4345/4405) bleiben komplett draußen – weder Saldo noch USt. Die Einnahme entsteht mit der Umbuchung vom Warte- auf das Erlöskonto bei Zahlung (siehe Abschnitt 9a).

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
```

### AfA-Berechnung

```
1. AssetCategories → Nutzungsdauer + Methode
2. Assets → Erfassung
3. AssetDepreciations → automatische AfA-Pläne
4. Buchungsintegration über Booking_ID: book_depreciation legt je Jahr genau eine
   Umbuchung „Aufwand an Anlagekonto“ an (Betrag positiv, ohne Account_ID, 31.12.),
   Sperre gegen Doppelbuchung in derselben Transaktion
5. EÜR: Section 3 (Aufwandskonto = −AfA); DATEV-Export: „Aufwand an Anlagekonto“, BU 0
```

Das Anlagekonto kommt aus dem Formular, sonst aus der Anlage oder ihrer Kategorie. Bei der
EÜR wird direkt abgeschrieben (Gegenkonto = Anlagekonto, kein Wertberichtigungskonto).
Aufwandskonto = Erfolgskonto (4000–7999) und Anlagekonto = Konto 1–999 werden geprüft, damit
kein Satz wie „6220 an 1460“ entsteht; nach dem Abgang der Anlage (`SaleDate`) wird keine AfA
mehr gebucht. Die zeitanteilige AfA im Abgangsjahr plant die Anwendung noch nicht (volle
Jahres-AfA, `docs/aufgaben.md`).
Hinweis GWG: Wer den Kauf schon auf das Aufwandskonto 6260 (Sofortabschreibung) gebucht hat
und zusätzlich die AfA bucht, zählt doppelt – das Programm prüft das nicht.

---

## Besonderheiten

1. **Foreign Keys:** `PRAGMA foreign_keys = ON`
2. **ON DELETE CASCADE:** InvoiceItems, AssetDepreciations, ContactAddresses, CompanyDetails, PersonDetails
   (nicht: `InvoiceCorrections`, `InvoiceDocumentVersions` – Protokoll und Fassungen überleben das Löschen eines Entwurfs)
3. **Snapshot-Prinzip:** Invoices kopieren Stammdaten bei Erstellung
4. **Datum-Format:** ISO 8601 (YYYY-MM-DD)
5. **Steuersätze:** Dezimal (0.19 = 19%)
6. **Seeding:** `_seed_chart_of_accounts()`, `_seed_asset_categories()`, `_seed_tax_keys()` laden aus `seed_data/`-JSON

---

**Dokumentversion:** 2.5
**Letzte Aktualisierung:** 12. November 2026 (Fassungen und Protokoll versendeter Rechnungen; Schema 2002)
