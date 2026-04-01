# Dokumentenparser & CSV-Import

## 1. PDF-Parser für Kontoauszüge

### Installation

```bash
pip install pdfplumber
```

Optional für OCR (gescannte PDFs):
```bash
pip install pytesseract pdf2image Pillow
```

Optional für PDF-Rechnungen mit Logo:
```bash
pip install Pillow
```

### Funktionsweise

1. **Upload**: PDF über Web-Oberfläche hochladen (Drag & Drop oder Dateiauswahl)
2. **Automatische Analyse**: Parser erkennt Kontoauszüge (z.B. Volksbank Rottweil)
   - IBAN zur Kontoidentifikation
   - Einzelne Transaktionen (Datum, Empfänger, Betrag, fremde IBAN)
3. **Organisation**: Dateien werden nach `./data/Belege/YYYY/Konten/VBR/` einsortiert
4. **Bestätigung**: Erkannte Transaktionen auf `/confirm_transactions` prüfen
5. **Import**: Nach Bestätigung als `BookingType='bank'` in Bookings-Tabelle

### Erweiterung für weitere Banken

In `document_parser.py` eine neue Methode hinzufügen:

```python
def parse_bank_statement_sparkasse(self, filepath: str) -> Dict:
    result = {'iban': None, 'document_date': None,
              'transactions': [], 'bank_code': 'Sparkasse'}
    # Parser-Logik
    return result
```

Erkennung in `parse_document()`:
```python
elif 'sparkasse' in text.lower():
    return self.parse_bank_statement_sparkasse(filepath)
```

---

## 2. WISO Mein Büro CSV-Import

### Übersicht

Zwei Export-Formate mit **automatischer Format-Erkennung**:

1. **Original-Export** (9 Spalten) → `BookingType='entry'`
2. **Tabellen-Export** (6 Spalten) → `BookingType='bank'`

Nach dem Import wird automatisch `link_bank_to_entries()` aufgerufen, das Bank- und Entry-Buchungen über `ParentBooking_ID` verknüpft.

---

### Format 1: Original-Export

**CSV-Format:**
```
ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
```

**Automatisches Mapping:**
- **KONTO** → `ChartOfAccounts.AccountNumber` → `COA_ID` (Sollkonto)
- **GEGENKONTO** → `ChartOfAccounts.AccountNumber` → `CounterCOA_ID` (Habenkonto)
- **SCHLUESSEL** → Steuersatz-Lookup über `TaxKeys`-Tabelle (50 BU-Schlüssel)
  - z.B. 9/401 → 19%, 8/402 → 7%, 121 → 0%, 490 → 0%
- **TaxAmount**: Automatisch berechnet als `|Brutto| - |Brutto| / (1 + TaxRate)`

**Liquiditäts-Erkennung (`_is_liquid()`):**
- Bankkonten: Über `Accounts.SKRAccount` JOIN mit ChartOfAccounts
- Kassenkonten: Kontonummer 1000–1099
- Verrechnungskonto: 1460
- Bestimmt Vorzeichen der Buchung

**Duplikat-Erkennung:** REFERENZNUMMER + Datum + COA_ID + Betrag

**BookingType:** `'entry'` (Buchungssatz)

---

### Format 2: Tabellen-Export

**Zweck:** Bankbewegungen mit Empfänger und Verwendungszweck importieren

**CSV-Format:**
```
Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr./opt. Beleg Nr.;Betrag
```

**Vorbereitung:**
1. Tabellen-Ansicht in WISO für ein Bank-/Verrechnungskonto öffnen
2. Als XLS exportieren → in Calc öffnen
3. Spalte 1 (Status) und Spalte 8 (Saldo) löschen
4. Als CSV mit Semikolon speichern

**Automatisches Mapping:**
- **Empf./Auft.** → `RecipientClient`
- **Verwendungszweck** → `Text` (Zeilenumbrüche → Leerzeichen)
- **Kategorie** → `COA_ID` (automatisches Matching über SKR-Beschreibung)
- **Beleg Nr.** → `DocumentNumber`
- **Konto-Nr. / IBAN** → `ForeignBankAccount` (falls vorhanden)

**Matching-Logik:**
- Sucht nach: Datum + Belegnummer + Betrag
- Gefunden → UPDATE (nur leere Felder ergänzen, keine Überschreibung)
- Nicht gefunden → INSERT

**BookingType:** `'bank'` (Bankbewegung)

---

### Format-Erkennung

Automatisch anhand der Spaltenüberschriften:
- **Original**: erkennt "KONTO" und "GEGENKONTO"
- **Tabelle**: erkennt "Empf./Auft." und "Verwendungszweck"

### Bank↔Entry-Verknüpfung

Nach jedem WISO-Import wird `link_bank_to_entries()` aufgerufen:
- Mehrstufiges Matching (Empfänger+Datum+Betrag → Datum+Betrag → Split-Summe)
- Doppik-Filter: Entry-Buchungen auf SKR-Bankkonten (z.B. 1810) werden ignoriert
- Ergebnis: `Entry.ParentBooking_ID → Bank.ID`

### Encoding

Automatische Erkennung: CP1252 (Standard), UTF-8-SIG, UTF-8, Latin-1

---

### Verwendung über Web-Interface

1. Navigieren zu `/miscellaneous`
2. "WISO Import" → CSV-Datei auswählen
3. Ergebnis: Anzahl importiert / aktualisiert / übersprungen / verknüpft

### Programmatische Verwendung

```python
from db import Database

db = Database()

with open('export_wiso.csv', 'rb') as f:
    csv_bytes = f.read()

result = db.import_wiso_csv(csv_bytes)
print(f"Importiert: {result['imported']}")
print(f"Aktualisiert: {result['updated']}")
print(f"Übersprungen: {result['skipped']}")
print(f"Fehler: {result['errors']}")

# Danach: Bank↔Entry verknüpfen
linked = db.link_bank_to_entries()
print(f"Verknüpft: {linked}")
```

### Empfohlener Import-Workflow

1. **SKR-Konten** prüfen (alle verwendeten müssen vorhanden sein)
2. **WISO Original-Export** importieren → Entry-Buchungen mit TaxRate + TaxAmount
3. **WISO Tabellen-Export** importieren → Bank-Buchungen mit Empfänger + Verwendungszweck
4. Automatische Verknüpfung wird nach jedem Import durchgeführt
5. Ergebnis unter `/transactions` prüfen (verknüpfte vs. offene Bankbuchungen)\n
