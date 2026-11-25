# Diagnose-MCP-Server und Demo-Modus

## Diagnose-MCP-Server (`tools/mcp_ubuchhaltung.py`)

- Nur Standardbibliothek, stdio; fragt eine laufende Instanz per HTTP ab.
- **Lesend per Konstruktion:** genau ein SELECT ohne `;` über `/execute_sql` (ohne `write`, also Lesemodus der Konsole – der Wächter bleibt die erste Schicht), nur lesende `pragma_`-Funktionen (`_READ_PRAGMAS`),
  kein WITH/RECURSIVE/zeroblob/randomblob; Seiten nur aus `_PAGE_ALLOWLIST` samt erlaubten Query-Schlüsseln. Lesewächter (`check_select`, `check_page_path`) nicht aufweichen; neue Seiten/Schlüssel nur für GET-Handler, die nichts schreiben (Nachweis im Test).
- Bewusst nicht in der Positivliste: `/backup/download` (ganzer Datenbestand), `/receipts/search`, `/transactions/search`, `/receipts/suggestions`, `/transactions/receipt-proposal`, `/transactions/suggest` (JSON-Suchen/Vorschläge), `/receipts/read` (POST, schreibt die Lesung),
  `/bwa/pdf`, `/bwa/csv`, `/euer/pdf`, `/susa/pdf`, `/susa/csv`, `/liquiditaet/pdf` (Dokumente).
- Zugangsdaten aus Windows-Anmeldeinformationsverwaltung, keyring oder Umgebung, nie im Repo; über `http://` nur an den eigenen Rechner, sonst nur mit `UBUCHHALTUNG_MCP_ALLOW_HTTP`.
  Registrierung nur lokal (`claude mcp add --scope local`), Einrichtung in `DEPLOYMENT.md`.

## Demo-Modus (`UBUCHHALTUNG_DEMO=1`, `demo.py`)

- Demodaten **ausschließlich unter `data/users/demo/`**, nur geladen, wenn das Verzeichnis fehlt oder leer ist und es keinen Benutzer `demo` gibt – **diese Garantie nie aufweichen**.
  Jeder Besucher arbeitet als `demo` (kein Administrator); `demo` ist wie `tests` ein reservierter Benutzername. Demodaten (`demo_seed.py`, `seed_data/test/demo_scenario.json`) entstehen über die App-eigenen Code-Pfade, relativ zum Startdatum; pytest-Testdaten bleiben getrennt.
- **Der Demo-Modus nimmt gar keine Datei eines Besuchers an** (403 vor dem Lesen, die Seite zeigt den Satz statt der Ablage/Dateiauswahl):
  - WISO-Datenbank (`wiso_upload.DEMO_REFUSED`), Belege `POST /receipts/upload` (`upload_handler.RECEIPTS_DEMO_REFUSED`), Kontoauszüge `POST /upload_receipts` (`upload_handler.STATEMENTS_DEMO_REFUSED`; `POST /confirm_transactions` ebenfalls 403, gelöscht wird nichts;
    `tests/test_statement_upload_demo.py`), Logo `POST /masterdata/contacts/upload-logo` (`handlers.LOGO_DEMO_REFUSED`), WISO-Textexport `POST /wiso/import` (`handlers.WISO_TEXT_DEMO_REFUSED`).
  - **Ein neuer Upload-Weg braucht dieselbe Sperre** (`tests/test_demo_uploads_closed.py` zählt die Wege auf).
- **Großer Bestand:** Neben dem handgepflegten Szenario (`demo_scenario.json`, unverändert) erzeugt `demo_history.build_history(today, profile)` mehrere Jahre – rein, ohne Datenbank, Zufall und Uhr (Auswahl je Monat über `zlib.crc32`,
  Tabellen `seed_data/test/demo_history.json`). Profil `full` = drei volle Vorjahre + laufendes Jahr (≈ 300 Rechnungen; `demo._seed` → `load_demo_scenario(db)`), `test` = ein Vorjahr.
  - `DemoScenario(db, today, extra=…, documents_days=…)` hängt Vorgaben an die Listen des Szenarios (ohne `extra` genau das Szenario) und lädt alle Rechnungsereignisse über EINE Warteschlange nach Datum; Nummern je Jahr in Datumsfolge,
    Rechnungen über den Weg der Maske (`handlers._invoice_fields`, `allocate_invoice_number`, `set_invoice_status`).
  - PDF/E-Rechnung nur für die letzten 30 Tage (`documents_days`); **in den letzten 14 Tagen legt der Erzeuger keine Rechnung an** (das E-Rechnungs-Beispiel `tools/make_einvoice_samples.py` bleibt die jüngste 19-%-Rechnung – nie ein Beleg mit Steuersatz je Position).
  - **Genau zwei Belege mit Steuersatz je Position** (`mixed_example`: 7 % und 19 %; `tax_entry_example`: 19 % und steuerfreie Position aus den Stammdaten, samt zwei Steuersatz-Einträgen steuerfrei und 5,5 %, die `DemoScenario.load_tax_entries` vorher anlegt; je in zwei Raten bezahlt, `taxMode: mixed`)
    **nur im Profil `full`** (`profiles.full.mixed_example`, `demo_history._mixed_invoice`): 45 Tage vor dem Stichtag, Werktag, nie im ersten Jahr, ohne Dokumente. Profil `test` (fest gepinnte Werte: EÜR-Schnappschüsse, Summen- und Saldenliste, Liquidität) und Handszenario bleiben ohne sie.
  - Erstes Jahr = Kleinunternehmerjahr; drei Bankbewegungen im laufenden Monat bleiben absichtlich offen. Für abgeschlossene Jahre bleiben Prüfliste, Splits, Übertragsprüfung und DATEV-Export ohne Befund – ändert sich der Erzeuger, die DATEN anpassen, nie die Prüfregeln. `tests/test_demo_history.py`
  - `load_test_seed_data(include_dated=False)` lässt fest datierte Buchungen, Anlagen, Angebote, Arbeitszeiten weg (Demo-Start); `convert_quote_to_invoice(quote_id, today=…)` nimmt den Tag der Demodaten.
- README-Bilder: `node tools/screenshots.mjs` → `docs/screenshots/` (hell + `*-dark.png`, README bindet beide per `<picture>`).
