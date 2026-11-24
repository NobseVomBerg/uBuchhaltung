# Diagnose-MCP-Server und Demo-Modus

## Diagnose-MCP-Server

- `tools/mcp_ubuchhaltung.py`: nur Standardbibliothek, stdio; Claude Code startet ihn lokal,
  er fragt eine laufende Instanz (z. B. den Docker-Container) per HTTP ab
- Lesend per Konstruktion: genau ein SELECT ohne `;` über `/execute_sql` (ohne `write`, also im Lesemodus der Konsole – der
  Wächter hier bleibt trotzdem die erste Schicht), nur lesende
  `pragma_`-Funktionen (`_READ_PRAGMAS`), kein WITH/RECURSIVE/zeroblob/randomblob; Seiten nur
  aus `_PAGE_ALLOWLIST` samt erlaubten Query-Schlüsseln – diesen Lesewächter (`check_select`,
  `check_page_path`) nicht aufweichen; neue Seiten und Schlüssel nur für GET-Handler, die
  nichts schreiben (Nachweis im Test). `/backup/download` steht bewusst nicht darin: Es liefert den ganzen Datenbestand
  (Gigabyte-Größe). `/receipts/search` und `/transactions/search` stehen bewusst nicht darin: JSON-Suchen der Seiten, keine
  Anzeigeseiten; dasselbe liefert die SQL-Abfrage. `/bwa/pdf`, `/bwa/csv`, `/euer/pdf`, `/susa/pdf`, `/susa/csv` und `/liquiditaet/pdf` stehen nicht darin (Dokumente).
  Ebenso nicht: `/receipts/suggestions` (JSON-Vorschläge der Seite eines Belegs) und
  `/receipts/read` (ein POST: liest die Datei eines Belegs und schreibt die Lesung)
- Zugangsdaten aus der Windows-Anmeldeinformationsverwaltung, keyring oder Umgebung, nie im Repo; über `http://` gehen sie nur
  an den eigenen Rechner, sonst nur mit `UBUCHHALTUNG_MCP_ALLOW_HTTP`. Registrierung nur
  lokal (`claude mcp add --scope local`), Einrichtung in `DEPLOYMENT.md`

## Demo-Modus

- `UBUCHHALTUNG_DEMO=1` (`demo.py`): Demodaten liegen **ausschließlich unter
  `data/users/demo/`** und werden nur geladen, wenn dieses Verzeichnis fehlt oder leer
  ist und es keinen Benutzer `demo` gibt – diese Garantie nie aufweichen. Solange die
  Variable gesetzt ist, arbeitet jeder Besucher als `demo` (kein Administrator); `demo`
  ist wie `tests` ein reservierter Benutzername. Demodaten (`demo_seed.py`,
  `seed_data/test/demo_scenario.json`) entstehen über die App-eigenen Code-Pfade,
  relativ zum Startdatum; pytest-Testdaten bleiben davon getrennt. Der Import aus der WISO-Datenbank ist im Demo-Modus
  abgeschaltet (Route 403, Handler und Seite mit demselben Satz, `wiso_upload.DEMO_REFUSED`): keine Dateien eines Besuchers
  auf dem Server. Ebenso das Hochladen von Belegen auf der Seite *Belege* (`POST /receipts/upload`: 403 vor dem Lesen,
  `upload_handler.RECEIPTS_DEMO_REFUSED`; die Seite zeigt den Satz statt der Ablage) und – seit 2.6.7 – das Hochladen von
  Kontoauszügen auf der Seite *Buchungen* (`POST /upload_receipts`: 403 vor dem Lesen, `upload_handler.STATEMENTS_DEMO_REFUSED`; die
  Seite zeigt den Satz statt der Ablage, `POST /confirm_transactions` antwortet dort ebenfalls 403 – ein Import aus der Zeit davor lässt
  sich im Demo-Modus nicht mehr bestätigen, gelöscht wird nichts; `tests/test_statement_upload_demo.py`) Seit 2.6.8 nimmt die App im
  Demo-Modus **gar keine Datei eines Besuchers** mehr an: auch der Logo-Upload (`POST /masterdata/contacts/upload-logo`: 403 vor dem
  Lesen, `handlers.LOGO_DEMO_REFUSED`) und der Import der Textexporte (`POST /wiso/import`: 403 vor dem Lesen,
  `handlers.WISO_TEXT_DEMO_REFUSED`) sind abgeschaltet, die Seiten zeigen den Satz statt der Dateiauswahl. Ein neuer Upload-Weg braucht
  dieselbe Sperre (`tests/test_demo_uploads_closed.py` zählt die Wege auf).
- **Der große Bestand (seit 2.5.10):** Neben dem von Hand gepflegten Szenario (`demo_scenario.json`, bleibt unverändert) erzeugt
  `demo_history.build_history(today, profile)` mehrere Jahre – rein, ohne Datenbank, Zufall und Uhr (Auswahl je Monat über
  `zlib.crc32`, Tabellen in `seed_data/test/demo_history.json`, Kunden und Artikel aus den Test-Seeddaten). Profil `full` =
  drei volle Vorjahre und das laufende Jahr (≈ 300 Rechnungen; `demo._seed` → `load_demo_scenario(db)`), `test` = ein Vorjahr.
  `DemoScenario(db, today, extra=…, documents_days=…)` hängt die Vorgaben an die Listen des Szenarios an (ohne `extra` entsteht
  genau das Szenario) und lädt alle Rechnungsereignisse über EINE Warteschlange nach Datum (Anlegen, Zahlung, Storno,
  Ersatzrechnung, Erstattung, Angebot umwandeln): Die Nummern entstehen je Jahr in Datumsfolge, die Rechnungen des Erzeugers
  über den Weg der Maske (`handlers._invoice_fields`, `allocate_invoice_number`, `set_invoice_status`). PDF/E-Rechnung gibt es
  nur für die letzten 30 Tage (`documents_days`), und **in den letzten 14 Tagen legt der Erzeuger keine Rechnung an** – das
  E-Rechnungs-Beispiel (`tools/make_einvoice_samples.py`) bleibt die jüngste 19-%-Rechnung des Szenarios – nie ein Beleg mit Steuersatz
  je Position. **Genau zwei solche Rechnungen** (7 % und 19 % auf einem Beleg, `mixed_example`; seit 2.8.0 dazu 19 % und eine steuerfreie Position aus den Stammdaten,
  `tax_entry_example`, samt zwei Einträgen in den Steuersätzen – steuerfrei und 5,5 % –, die `DemoScenario.load_tax_entries` vor den Rechnungen anlegt; je in zwei Raten bezahlt, `taxMode: mixed` im Formular) gibt es
  **nur im Profil `full`** (`profiles.full.mixed_example` der Tabelle, `demo_history._mixed_invoice`): 45 Tage vor dem Stichtag auf einem Werktag, nie
  im ersten Jahr, ohne Dokumente (älter als 30 Tage); das Profil `test` – der Bestand der fest gepinnten Werte (EÜR-Schnappschüsse, Summen- und
  Saldenliste, Liquidität) – und das handgepflegte Szenario bleiben ohne sie. Das erste Jahr ist ein
  Kleinunternehmerjahr; drei Bankbewegungen im laufenden Monat bleiben absichtlich offen (der DATEV-Export des laufenden Jahres
  fragt deshalb nach, abgeschlossene Jahre gehen ohne Hinweis durch). Für abgeschlossene Jahre bleiben Prüfliste, Splits,
  Übertragsprüfung und DATEV-Export ohne Befund – ändert sich der Erzeuger, die DATEN anpassen, nie die Prüfregeln
  (`tests/test_demo_history.py`). `load_test_seed_data(include_dated=False)` lässt die fest datierten Buchungen, Anlagen, Angebote und
  Arbeitszeiten weg (der Demo-Start nutzt es), `convert_quote_to_invoice(quote_id, today=…)` nimmt den Tag der Demodaten
- README-Bilder neu erzeugen: `node tools/screenshots.mjs` → `docs/screenshots/`
  (hell + `*-dark.png`, README bindet beide per `<picture>` ein)
