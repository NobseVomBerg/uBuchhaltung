# Dokumentenparser & CSV-Import Installation & Verwendung

## Installation

### 1. PDF-Parser für Kontoauszüge

Um die automatische Dokumentenanalyse zu nutzen, installieren Sie die erforderlichen Pakete:

```bash
pip install pdfplumber
```

### Für PDF-Rechnungserstellung mit Logo

```bash
pip install Pillow
```

Pillow wird benötigt, um Firmenlogos (PNG/JPEG) in die PDF-Rechnung einzubetten. Legen Sie Ihre Logos im `static/` Verzeichnis ab (z.B. `static/logo.png`, `static/firma2.png`). Die Logos werden in den Kontakten (Typ: "Eigene Daten") über den File-Picker ausgewählt und automatisch in Rechnungen verwendet.

### Optional: OCR-Unterstützung für gescannte PDFs

```bash
pip install pytesseract pdf2image Pillow

# Windows: Tesseract OCR herunterladen von:
# https://github.com/UB-Mannheim/tesseract/wiki
```

## Funktionsweise

### 1. Dokumenten-Upload
Dateien können über die Web-Oberfläche hochgeladen werden (Drag & Drop oder Dateiauswahl).

### 2. Automatische Analyse
Der Parser erkennt automatisch:
- **Kontoauszüge** (z.B. Volksbank Rottweil)
  - IBAN zur Kontoidentifikation
  - Belegdatum
  - Einzelne Transaktionen mit:
    - Buchungsdatum
    - Empfänger/Auftraggeber
    - Verwendungszweck
    - Betrag (Soll/Haben)
    - Fremde IBAN

- **Rechnungen** (geplant)
  - Rechnungsdatum
  - Rechnungsnummer
  - Beträge
  - Lieferant

### 3. Automatische Organisation
Dateien werden automatisch in folgende Struktur einsortiert:

```
./data/Belege/
├── 2025/
│   ├── Konten/
│   │   ├── VBR/          # Volksbank Rottweil
│   │   └── Sparkasse/
│   └── Rechnungen/
└── 2026/
    └── Konten/
        └── VBR/
```

## Erweiterung für weitere Banken

Um einen Parser für eine weitere Bank hinzuzufügen, erweitern Sie `document_parser.py`:

```python
def parse_bank_statement_sparkasse(self, filepath: str) -> Dict:
    """Parse Sparkasse bank statement"""
    result = {
        'iban': None,
        'document_date': None,
        'transactions': [],
        'bank_code': 'Sparkasse'
    }
    
    # Ihre Parser-Logik hier
    
    return result
```

Und fügen Sie die Erkennung in `parse_document()` hinzu:

```python
elif 'sparkasse' in text.lower():
    return self.parse_bank_statement_sparkasse(filepath)
```

## Manuelle Analyse

Sie können Dokumente auch manuell analysieren:

```python
from document_parser import DocumentParser

parser = DocumentParser()
result = parser.parse_document('./data/Belege/kontoauszug.pdf')
```

---

## 2. WISO Mein Büro CSV-Import

### Funktionsweise

Der CSV-Import ermöglicht den direkten Import von Buchungen aus WISO Mein Büro:

**CSV-Format:**
```
ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
```

**Automatisches Mapping:**
- **KONTO** → ChartOfAccounts.AccountNumber → COA_ID (Sollkonto)
- **GEGENKONTO** → ChartOfAccounts.AccountNumber → CounterCOA_ID (Habenkonto)
- **SCHLUESSEL** → BU-Schlüssel → TaxRate
  - 401 = 19% Umsatzsteuer
  - 402 = 7% Umsatzsteuer
  - 121 = 0% (steuerfrei)

**Duplikat-Erkennung:**
- Prüfung anhand: REFERENZNUMMER + Datum + COA_ID + Betrag
- Datum ist wichtig für wiederkehrende Transaktionen (z.B. monatliche Abos)
- Bereits vorhandene Buchungen werden übersprungen

**Fehlerbehandlung:**
- Fehlende SKR-Konten werden gemeldet (KONTO und GEGENKONTO)
- Duplikate werden mit Details aufgelistet
- Fehlerhafte Zeilen werden protokolliert

**Encoding:**
- Automatische Erkennung: CP1252 (Standard), UTF-8-SIG, UTF-8, Latin-1

### Verwendung über Web-Interface

1. Navigieren Sie zu `/miscellaneous`
2. Klicken Sie auf "WISO Import"
3. Wählen Sie die CSV-Datei aus
4. Überprüfen Sie das Import-Ergebnis:
   - Anzahl importierter Buchungen
   - Liste übersprungener Duplikate
   - Fehlende SKR-Konten (müssen zuerst angelegt werden)
   - Fehlerhafte Zeilen

### Programmatische Verwendung

```python
from db import Database

db = Database()

# CSV-Datei einlesen
with open('export_wiso.csv', 'rb') as f:
    csv_bytes = f.read()

# Import durchführen
result = db.import_wiso_csv(csv_bytes)

print(f"Importiert: {result['imported']}")
print(f"Übersprungen: {result['skipped']}")
print(f"Fehlende Konten: {result['missing_coa']}")
print(f"Fehlende Gegenkonten: {result['missing_counter_coa']}")
print(f"Fehler: {result['errors']}")
```

### Vorbereitung

**Vor dem Import sicherstellen:**
1. Alle verwendeten SKR-Konten sind in ChartOfAccounts angelegt
2. Sowohl KONTO als auch GEGENKONTO müssen vorhanden sein
3. Bei fehlenden Konten erscheint eine Liste mit den fehlenden Kontonummern
4. Diese können unter `/skr` nachträglich angelegt werden

**Empfohlener Workflow:**
1. Ersten Import-Versuch starten
2. Liste fehlender Konten notieren
3. Fehlende Konten unter `/skr` anlegen
4. Import erneut durchführen

print(f"IBAN: {result['iban']}")
print(f"Datum: {result['document_date']}")
print(f"Transaktionen: {len(result['transactions'])}")
```

## Datenbank-Integration

Die extrahierten Transaktionen werden automatisch in die `Bookings`-Tabelle eingefügt:

1. **Upload**: PDF-Datei über Web-Interface hochladen
2. **Parsing**: `DocumentParser` analysiert VBR-Kontoauszug
3. **Organisation**: Datei wird nach `./data/Belege/YYYY/Konten/VBR/` verschoben
4. **Bestätigung**: Benutzer prüft erkannte Transaktionen auf `/confirm_transactions`
5. **Import**: Nach Bestätigung werden Transaktionen in DB gespeichert
6. **Duplikat-Check**: Bereits existierende Transaktionen werden übersprungen (basierend auf Datum, Betrag, Konto, fremde IBAN und Verwendungszweck)

### Buchungsfelder beim Import

Die folgenden Felder werden beim Import automatisch gesetzt:
- **DateBooking**: Buchungsdatum aus Kontoauszug
- **DateTax**: Gleich wie DateBooking (kann später manuell angepasst werden)
- **Account_ID**: Identifiziert durch IBAN-Matching
- **ForeignBankAccount**: Fremde IBAN oder Kontonummer
- **RecipientClient**: Empfänger/Auftraggeber
- **Amount**: Betrag (positiv für Eingänge, negativ für Ausgänge)
- **Currency**: 'EUR' (Standard)
- **Text**: Verwendungszweck
- **Status**: 'draft' (Entwurf) - muss manuell auf 'posted' gesetzt werden
- **BookingType**: Automatisch bestimmt ('income' für positive, 'expense' für negative Beträge)

Weitere Felder können nach dem Import manuell ergänzt werden:
- Contact_id (Kunde/Lieferant)
- COA_ID (SKR-Kontenzuordnung)
- Category_ID (Kategorie)
- TaxRate und TaxAmount (Steuerberechnung)
- DocumentNumber (Belegnummer)
- BookingGroup_ID (für Split-Buchungen)

### Integration in modularer Struktur

Der Parser ist in `server/upload_handler.py` integriert:
```python
from document_parser import DocumentParser

def handle_file_upload(request_handler):
    parser = DocumentParser()
    new_path, parsed_data = parser.process_and_organize(filepath)
    
    # Transaktionen für Bestätigung speichern
    import_id = parser.save_parsed_data(filename, parsed_data)
    # Benutzer zu Bestätigungsseite weiterleiten
```

Die Bestätigung erfolgt über `server/handlers.py` → `handle_confirm_import()`, welches die Methoden
`db.check_booking_exists()` und `db.insert_booking()` verwendet.

## Bekannte Einschränkungen

- **VBR-Parser**: Aktuell speziell für Volksbank Rottweil Kontoauszüge optimiert
- **PDF-Format**: Funktioniert am besten mit Text-PDFs (nicht gescannt)
- **Tabellenstruktur**: Parser erwartet bestimmte Spaltenüberschriften

## Alternative: Lokale KI

Für komplexere Dokumente oder wenn der Parser nicht funktioniert, kann eine lokale KI verwendet werden:

```bash
# Ollama installieren (siehe https://ollama.ai)
ollama pull llava

# Dann in Python:
# from ollama import Client
# client = Client()
# response = client.generate(model='llava', prompt='Analyse diesen Kontoauszug...', images=['path/to/image'])
```
