"# PyBuch

Eine einfache webbasierte Buchhaltungsanwendung für kleine Unternehmen und Selbstständige.

## Überblick

PyBuch ist eine schlanke, webbasierte Buchhaltungssoftware, die in Python entwickelt wurde. Sie ermöglicht die Verwaltung von Belegen, Konten, Zahlungen und nutzt den deutschen Standardkontenrahmen (SKR).

## Features

### 1. Belege-Verwaltung (`/belege`)
Verwalten Sie Ihre Buchhaltungsbelege:
- **Anzeigen**: Übersicht aller Belege mit Nummer, Datum, Dateiname, Pfad und zusätzlichen Informationen
- **Hinzufügen**: Neue Belege mit eindeutiger Nummer erfassen
- **Bearbeiten**: Bestehende Belege aktualisieren
- **Datenspeicherung**: SQLite-Datenbank mit UNIQUE-Constraints für Belegnummer und Dateiname/Pfad-Kombinationen

### 2. Konten-Verwaltung (`/konten`)
Verwalten Sie Ihre Bankkonten und Kasse:
- **Anzeigen**: Übersicht aller Konten mit Bezeichnung, Inhaber, IBAN, BIC und Bankname
- **Hinzufügen**: Neue Bankkonten anlegen
- **Bearbeiten**: Bestehende Konten aktualisieren
- **Löschen**: Konten entfernen (außer Kasse)
- **Kasse**: Spezielle Sonderform für Bargeldverwaltung
  - Automatisch beim ersten Start angelegt
  - Kann nicht gelöscht oder bearbeitet werden
  - Immer als erstes in der Liste angezeigt

**Kontenfelder:**
- Bezeichnung (Pflichtfeld)
- Inhaber
- IBAN
- BIC
- BankName
- Typ (Kasse/Bank)

### 3. Standardkontenrahmen (SKR) (`/skr`)
Verwaltung des Kontenrahmens nach deutschem Standard:
- **SKR 03/04**: Deutschland
- **SKR 07**: Österreich
- **Anzeigen**: Übersicht aller SKR-Einträge mit RahmenNr, Konto, Name und Gruppe
- **Hinzufügen**: Neue Konten zum Rahmen hinzufügen
- **Bearbeiten**: Bestehende Einträge aktualisieren
- **UNIQUE-Constraint**: Kombination aus RahmenNr und Kontonummer muss eindeutig sein

### 4. About-Seite (`/about`)
Informationen über die Anwendung.

## Technische Details

### Datenbankstruktur

Die Anwendung verwendet SQLite mit folgenden Tabellen:

**Belege**
- Nummer (TEXT, UNIQUE)
- Datum (DATE)
- Dateiname (TEXT)
- Pfad (TEXT)
- Info (TEXT)
- UNIQUE(Dateiname, Pfad)

**Konten**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Bezeichnung (TEXT NOT NULL, UNIQUE)
- Inhaber (TEXT)
- IBAN (TEXT)
- BIC (TEXT)
- BankName (TEXT)
- IstKasse (INTEGER DEFAULT 0)

**Skr (Standardkontenrahmen)**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- RahmenNr (INTEGER) - z.B. 03, 04 oder 07
- Konto (INTEGER)
- Name (TEXT)
- Gruppe (TEXT)
- UNIQUE(RahmenNr, Konto)

**Zahlung**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- Datum1 (DATE)
- Datum2 (DATE)
- BankEigen (TEXT)
- BankFremd (TEXT)
- Zweck (TEXT)
- BelegNummer (TEXT)
- Betrag (REAL)
- SkrBuchJoinId (INTEGER)

**SkrBuch**
- ID (INTEGER PRIMARY KEY AUTOINCREMENT)
- JoinId (INTEGER)
- KntNr (TEXT)
- BetragNetto (REAL)
- Steuer (REAL)

### Projektstruktur

```
PyBuch/
├── main.py                    # Entry Point - Startet den Webserver
├── db.py                      # Datenbank-Layer mit allen CRUD-Operationen
├── document_parser.py         # PDF-Parser für Kontoauszüge (VBR)
├── buch.css                   # Stylesheet für die Weboberfläche
├── README.md                  # Diese Datei
├── PARSER_README.md           # Dokumentation für den PDF-Parser
├── requirements_parser.txt    # Python-Abhängigkeiten für Parser
├── server/                    # Modularer Webserver (refactored)
│   ├── __init__.py           # Package initialization
│   ├── app.py                # HTTP-Server-Klasse mit Routing
│   ├── pages.py              # HTML-Seiten-Generierung (alle 15 Seiten)
│   ├── handlers.py           # POST-Request-Handler für Formulare
│   └── upload_handler.py     # File-Upload mit PDF-Parsing
└── data/                      # Daten-Verzeichnis (im .gitignore)
    ├── buch.db               # SQLite-Datenbank (automatisch erstellt)
    ├── Belege/               # Hochgeladene Belege
    │   └── 2025/
    │       └── Konten/
    │           └── VBR/      # Organisierte Bank-Statements
    └── pending_imports/      # Temp. Transaktionen vor Bestätigung
```

## Installation und Start

### Voraussetzungen
- Python 3.x
- Keine externen Abhängigkeiten (nutzt nur Python Standard Library)

### Server starten

```bash
python main.py
```

Der Server startet standardmäßig auf `http://localhost:8080`

### Alternative Konfiguration

```python
# In server/app.py die run_server() Funktion anpassen:
run_server(host="0.0.0.0", port=8000)  # Für anderen Host/Port
```

### PDF-Parser aktivieren (optional)

```bash
pip install -r requirements_parser.txt
```

Damit werden VBR-Kontoauszüge automatisch geparst und Transaktionen importiert.

## Verwendung

1. **Server starten**: `python main.py`
2. **Browser öffnen**: Navigieren Sie zu `http://localhost:8080`
3. **Initialisierung**: Klicken Sie auf "Initialize DB Content" um Testdaten zu laden (optional)
4. **Navigation**: Nutzen Sie das Menü zur Navigation zwischen den verschiedenen Bereichen
5. **PDF-Upload**: Laden Sie VBR-Kontoauszüge hoch - Transaktionen werden automatisch erkannt
6. **Import bestätigen**: Prüfen Sie erkannte Transaktionen und bestätigen Sie den Import

## Sicherheitshinweise

Diese Anwendung ist für lokale Verwendung konzipiert:
- ⚠️ Keine Benutzer-Authentifizierung
- ⚠️ Keine Verschlüsselung
- ⚠️ Keine Input-Validierung gegen SQL-Injection (verwendet parametrisierte Queries)
- ⚠️ Nicht für den produktiven Einsatz im Internet geeignet

## Entwicklung

### Architektur
- **Webserver**: Python's `http.server.BaseHTTPRequestHandler`
- **Modularer Aufbau**: Separation of Concerns
  - `server/app.py`: Routing und HTTP-Handler
  - `server/pages.py`: HTML-Generierung (15 Seiten)
  - `server/handlers.py`: Form-Verarbeitung (POST)
  - `server/upload_handler.py`: File-Upload mit Multipart-Parsing
- **Datenbank**: SQLite mit `sqlite3` Modul
- **PDF-Parsing**: pdfplumber für VBR-Kontoauszüge
- **Frontend**: Server-seitig generiertes HTML mit minimalem JavaScript (Drag & Drop)
- **Styling**: Externes CSS (`buch.css`)

### Erweiterungen
Die modulare Struktur erlaubt einfache Erweiterungen:
- **Neue Seiten**: Fügen Sie Funktionen in `server/pages.py` hinzu (z.B. `PageXXX()`)
- **Neue Routes**: Erweitern Sie `do_GET()` in `server/app.py`
- **Neue Handler**: Fügen Sie Funktionen in `server/handlers.py` hinzu
- **Neue Tabellen**: Erweitern Sie `db.py` mit entsprechenden CRUD-Methoden
- **Neue Parser**: Erweitern Sie `document_parser.py` für weitere Banken

### Vorteile der Modularisierung
- ✅ Kleinere Dateien (~150-750 Zeilen statt 1048)
- ✅ Klare Trennung der Verantwortlichkeiten
- ✅ Bessere KI/Copilot-Unterstützung (vollständiger Kontext)
- ✅ Einfachere Wartung und Debugging
- ✅ Parallel-Entwicklung möglich

## Lizenz

Dieses Projekt ist für Lern- und Demonstrationszwecke erstellt.

## Autor

NobseVomBerg
" 
