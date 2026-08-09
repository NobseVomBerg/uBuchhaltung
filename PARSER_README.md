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

### Aufbau: Abstraktionsschicht + Bankmodule

Der Import ist zweigeteilt. Die Anwendung kennt nur die Abstraktionsschicht,
die Bankeigenheiten stecken je in einem eigenen Modul unter `importers/`:

| Datei | Aufgabe |
|---|---|
| `importers/base.py` | Gemeinsames Datenmodell: `BankStatement` mit `StatementTransaction`-Zeilen |
| `importers/pdftext.py` | Extraktions-Helfer für alle Module: Text, IBAN, Belegdatum |
| `importers/plausibility.py` | Zentrale Inhaltsprüfung (siehe unten) |
| `importers/vbr.py` | Volksbank Rottweil (PDF) |
| `importers/dkb.py` | DKB (PDF) |
| `importers/__init__.py` | Registry `IMPORTERS`, `find_importer()`, `parse_statement()` |

Die Spalten einer importierten Bewegung (`CANONICAL_FIELDS`) sind bewusst
knapp: `date`, `amount`, `recipient`, `reference`, `foreign_iban`. Mehr braucht
eine Bankbewegung nicht, und mehr muss ein Bankmodul nicht liefern.

**Neue Bank anbinden:** ein Modul mit einer `StatementImporter`-Klasse
schreiben (`detect()` erkennt den Beleg, `parse()` liefert den
`BankStatement`) und die Klasse in `IMPORTERS` eintragen. Am übrigen Code
ändert sich nichts.

### Plausibilitätsprüfung

PDF-Textextraktion ist ungenau: Kopf- und Fußzeilen rutschen in Empfänger oder
Verwendungszweck, Beträge verschwinden, Jahreszahlen verrutschen. Nach dem
Parsen läuft deshalb `plausibility.check_statement()` über jeden Beleg und
vermerkt Befunde an den Bewegungen – ohne den Import zu blockieren; die
Vorschau zeigt sie, entschieden wird vom Nutzer.

| Befund | Bedeutung |
|---|---|
| `amount` | Betrag fehlt oder ist 0 |
| `date` | Datum fehlt oder ist unlesbar |
| `daterange` | Datum liegt weit neben dem Belegdatum (Jahresdreher) |
| `empty` | weder Empfänger noch Verwendungszweck |
| `boiler` | Inhalt ist vollständig Kopf-/Fußzeilentext ("Seite 2 von 5", "Übertrag auf Blatt 2", "Neuer Kontostand", Formularnummern) |
| `huge` | Betrag über 1.000.000 – fast immer ein verrutschtes Trennzeichen |

Am Beleg selbst: `no_transactions`, `no_iban`, `parse_error`.

Die `boiler`-Muster greifen nur auf das **ganze** Feld: "Kontoauszug-Gebühr
Januar" oder "Blattwerk Gartenbau" bleiben unauffällig, "Blatt 3" nicht. Und
eine Zeile mit brauchbarem Verwendungszweck wird nicht wegen eines kruden
Empfängers markiert.

### Funktionsweise

1. **Upload**: PDF über Web-Oberfläche hochladen (Drag & Drop oder Dateiauswahl)
2. **Automatische Analyse**: das zuständige Importmodul erkennt den Beleg selbst
   - IBAN zur Kontoidentifikation
   - Einzelne Transaktionen (Datum, Empfänger, Betrag, fremde IBAN)
   - Plausibilitätsprüfung markiert Auffälligkeiten
3. **Organisation**: Dateien werden nach `./data/Belege/YYYY/Konten/<BANK>/` einsortiert
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
- **Spezialfall 4405→4400**: Wenn kein BU-Schlüssel vorhanden ist,
    wird automatisch 19% gesetzt und TaxAmount berechnet

**Liquiditäts-Erkennung (`_is_liquid()`):**
- Bankkonten: Über `Accounts.SKRAccount` JOIN mit ChartOfAccounts
- Kassenkonten: Kontonummer 1000–1099
- Verrechnungskonto: 1460
- Bestimmt Vorzeichen der Buchung

**Duplikat-Erkennung (zählbasiert):** REFERENZNUMMER + Datum + COA_ID + Betrag;
ohne Referenznummer: Datum + COA_ID + Betrag (nur Entry-Buchungen, ohne Text —
der Tabellen-Export überschreibt ihn nachträglich). Übersprungen werden je
Schlüssel nur so viele Zeilen, wie die DB bereits enthält — gleichartige
Split-Positionen (gleicher Betrag/Konto/Tag) werden dadurch korrekt importiert.

**BookingType:** `'entry'` (Buchungssatz)

---

### Format 2: Tabellen-Export

**Zweck:** Bankbewegungen mit Empfänger und Verwendungszweck importieren

**CSV-Format (6 Spalten, Standard):**
```
Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr./opt. Beleg Nr.;Betrag
```

**CSV-Format (7 Spalten, erweiterter Export mit IBAN):**
```
Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag
```

Beide Varianten werden automatisch erkannt (Format-Erkennung über `Empf./Auft.` + `Verwendungszweck`).

**Vorbereitung:**
1. Tabellen-Ansicht in WISO für ein Bank-/Verrechnungskonto öffnen
2. Als XLS exportieren → in Calc öffnen
3. Spalte 1 (Status) und letzte Spalte (Saldo) löschen
4. Als CSV mit Semikolon speichern

**Automatisches Mapping:**
- **Empf./Auft.** → `RecipientClient`
- **Verwendungszweck** → `Text` (Zeilenumbrüche → Leerzeichen)
- **Kategorie** → `COA_ID` (automatisches Matching über SKR-Beschreibung)
- **Beleg Nr. / opt. Beleg Nr.** → `DocumentNumber`
- **Konto-Nr. / IBAN** → `ForeignBankAccount` (optionale Spalte, falls vorhanden)

**Matching-Logik (mehrstufig):**

Bei **Buchungen mit Belegnummer** (`Beleg Nr.` gesetzt):
- Stufe 1 (direkt): Datum + Belegnummer + Betrag → genau ein Treffer
- Stufe 2 (Bank-Parent): Bankbuchung mit gleichem Datum/Betrag suchen, Entry-Kinder per Belegnummer ergänzen
- Stufe 3 (Beleg-Split): Summenabgleich über alle Buchungen gleicher Belegnummer

Bei **Buchungen ohne Belegnummer** (`Beleg Nr.` leer):
- Stufe A (bank+entry-Paar): Wenn Datum+Betrag genau eine bank- und eine entry-Buchung treffen und die entry-Buchung Child der bank-Buchung ist → beide gemeinsam aktualisieren (Realfall: Privatentnahmen, Bankgebühren, Zinsen)
- Stufe B (Text-Disambiguierung): Verwendungszweck-Normalisierung als Tiebreaker

Gefunden → UPDATE (RecipientClient, ForeignBankAccount, COA_ID ergänzen — nur leere Felder)  
Nicht gefunden → in `not_found`-Liste

**BookingType:** `'bank'` (Bankbewegung)

---

### Format-Erkennung

Automatisch anhand der Spaltenüberschriften:
- **Original**: erkennt "KONTO" und "GEGENKONTO"
- **Tabelle**: erkennt "Empf./Auft." und "Verwendungszweck"

### Bank↔Entry-Verknüpfung (link_bank_to_entries)

Nach jedem WISO-Import wird `link_bank_to_entries()` aufgerufen:
- Mehrstufiges Matching:
    - Stufe 1: Datum + normalisierter Empfänger + Betrag
    - Stufe 2: Datum + Betrag (eindeutig nach Doppik-Filter)
    - Stufe 3: Split-Gruppen mit Summenabgleich (nur gleicher Tag)
    - Stufe 3b: Rechnungs-Split (SUM/Anzahl, Bank-COA als Marker)
    - Stufe 3c: Privatanteil-Split (Summe minus Privatentnahme-Offset)
    - Stufe 3d: Sammelzahlung (mehrere Rechnungsnummern im Bank-Text)
    - Stufe 4: DocumentNumber-Tiebreaker bei Mehrdeutigkeit
    - Stufe 5: Text-Token-Matching (lange Ziffernfolgen ≥ 8 Stellen)
    - Stufe 6: Text-Similarity ohne Belegnummer (`SequenceMatcher`)
    - Stufe 7: Debitoren-Auflösung (`Status='resolved'` für Debitoren-Entries, deren Zahlung bereits verknüpft ist)
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
5. Ergebnis unter `/transactions` prüfen (verknüpfte vs. offene Bankbuchungen)
