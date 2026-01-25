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

Die extrahierten Transaktionen können in die `Zahlung`-Tabelle eingefügt werden.
Dies ist aktuell noch nicht automatisch implementiert, kann aber leicht ergänzt werden.

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
