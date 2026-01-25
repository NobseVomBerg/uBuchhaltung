# Dokumentenparser Installation & Verwendung

## Installation

Um die automatische Dokumentenanalyse zu nutzen, installieren Sie die erforderlichen Pakete:

```bash
pip install pdfplumber
```

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

print(f"IBAN: {result['iban']}")
print(f"Datum: {result['document_date']}")
print(f"Transaktionen: {len(result['transactions'])}")
```

## Datenbank-Integration

Die extrahierten Transaktionen werden automatisch in die `Zahlung`-Tabelle eingefügt:

1. **Upload**: PDF-Datei über Web-Interface hochladen
2. **Parsing**: `DocumentParser` analysiert VBR-Kontoauszug
3. **Organisation**: Datei wird nach `./data/Belege/YYYY/Konten/VBR/` verschoben
4. **Bestätigung**: Benutzer prüft erkannte Transaktionen auf `/confirm_transactions`
5. **Import**: Nach Bestätigung werden Transaktionen in DB gespeichert
6. **Duplikat-Check**: Bereits existierende Transaktionen werden übersprungen

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

Die Bestätigung erfolgt über `server/handlers.py` → `handle_confirm_import()`.

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
