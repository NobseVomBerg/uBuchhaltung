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
├── WebServer.py      # Haupt-Webserver mit HTTP-Routing und HTML-Generierung
├── db.py             # Datenbank-Layer mit allen CRUD-Operationen
├── buch.css          # Stylesheet für die Weboberfläche
├── README.md         # Diese Datei
└── buch.db           # SQLite-Datenbank (wird automatisch erstellt)
```

## Installation und Start

### Voraussetzungen
- Python 3.x
- Keine externen Abhängigkeiten (nutzt nur Python Standard Library)

### Server starten

```bash
python WebServer.py
```

Der Server startet standardmäßig auf `http://localhost:8080`

### Alternative Konfiguration

```python
# In WebServer.py am Ende der Datei:
if __name__ == "__main__":
    run_server(host="0.0.0.0", port=8000)  # Für anderen Host/Port
```

## Verwendung

1. **Server starten**: `python WebServer.py`
2. **Browser öffnen**: Navigieren Sie zu `http://localhost:8080`
3. **Initialisierung**: Klicken Sie auf "Initialize DB Content" um Testdaten zu laden (optional)
4. **Navigation**: Nutzen Sie das Menü zur Navigation zwischen den verschiedenen Bereichen

## Sicherheitshinweise

Diese Anwendung ist für lokale Verwendung konzipiert:
- ⚠️ Keine Benutzer-Authentifizierung
- ⚠️ Keine Verschlüsselung
- ⚠️ Keine Input-Validierung gegen SQL-Injection (verwendet parametrisierte Queries)
- ⚠️ Nicht für den produktiven Einsatz im Internet geeignet

## Entwicklung

### Architektur
- **Webserver**: Python's `http.server.BaseHTTPRequestHandler`
- **Datenbank**: SQLite mit `sqlite3` Modul
- **Frontend**: Server-seitig generiertes HTML ohne JavaScript-Frameworks
- **Styling**: Externes CSS (`buch.css`)

### Erweiterungen
Die modulare Struktur erlaubt einfache Erweiterungen:
- Neue Seiten: Fügen Sie Methoden wie `PageXXX()` hinzu
- Neue Routen: Erweitern Sie `do_GET()` oder `do_POST()`
- Neue Tabellen: Erweitern Sie `db.py` mit entsprechenden CRUD-Methoden

## Lizenz

Dieses Projekt ist für Lern- und Demonstrationszwecke erstellt.

## Autor

NobseVomBerg
" 
