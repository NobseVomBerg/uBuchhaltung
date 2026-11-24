# Schema-Migrationen und Projektzahlen

## Versionierung

- **Bei jedem Versionssprung `python tools/project_stats.py` ausführen und `project_stats.json` mit committen:** Die Datei trägt die Zahlen
  von „Projekt in Zahlen“ auf der About-Seite (Dateien, Zeilen und Zeilen ohne Leerzeilen und Kommentare für Programm, Tests und
  Beispieldaten, dazu Testfunktionen und Testfälle) und die Version, aus der sie stammen; `tests/test_project_stats.py` verlangt, dass
  sie zu `APP_VERSION` gehört (`python tools/project_stats.py --check` prüft nur das). Das Werkzeug zählt im Projektordner (`os.walk`, nur
  Standardbibliothek, ohne `git`; Auswahl und Ausschlüsse stehen als Daten oben im Skript), sammelt die Testfälle mit
  `pytest --collect-only` ein – scheitert das, schreibt es nichts – und schreibt weder Datum noch Pfade: Die Datei ändert sich nur, wenn sich
  das Projekt ändert. Docstrings zählen als Kommentar. Die About-Seite liest nur diese Datei (`server/project_stats.py`, streng: höchstens
  64 KB, ganze Zahlen von 0 bis 10⁹, die Version aus `[0-9A-Za-z.+-]`; jede Abweichung ergibt `None` für die ganze Datei, und der Abschnitt
  fehlt), zur Laufzeit wird nichts gezählt – im Docker-Image fehlt `tests/`. Gehört die Datei nicht zur Version des Programms, sagt die Seite es
  („Die Zahlen stammen aus Version …“)
- Migration auf 2.2.0 (`INVOICE_VERSIONS_SCHEMA`, Schema 2002): hängt `Invoices` fünf Spalten an (`InvoiceKind`,
  `CancelsInvoiceId`, `ReplacesInvoiceId`, `SentAt`, `DocumentVersion`; die ersten drei sind für den Stornobeleg
  angelegt – `InvoiceKind` und `CancelsInvoiceId` nutzt er seit 2.2.1, `ReplacesInvoiceId` die Ersatzrechnung seit 2.2.2,
  beide ohne Schemaänderung und `ReplacesInvoiceId` ohne Index) und legt `InvoiceCorrections`, `InvoiceDocumentVersions` und den eindeutigen Index
  auf `CancelsInvoiceId` an – ebenfalls nach der Sicherung und in einer Transaktion. Der Bestand behält überall
  NULL (`DocumentVersion` NULL = Fassung 1), nichts wird umgedeutet. Frische und migrierte Datenbank haben dieselbe
  Spaltenreihenfolge (`InvoiceCol`, Index 50–54). Die beiden neuen Tabellen haben bewusst keinen Fremdschlüssel:
  Protokoll und Fassungen überleben das Löschen eines Entwurfs
- Migration auf 2.3.0 (`DATEV_KEY_SCHEMA`, Schema 2003): hängt `Bookings.DatevKey` (Index 23, NULL = keiner mitgebracht) und
  `CompanyDetails.DatevConsultantNo`/`DatevClientNo` an – nach der Sicherung und in EINER Transaktion – und berichtigt `TaxKeys`
  (`TAX_KEY_CORRECTIONS`: 100/120/121/122/490 – eine abweichende Zeile wird an ihrer Stelle überschrieben, eine fehlende angelegt, eine richtige
  bleibt unberührt; nur wenn die Tabelle Zeilen hat, die leere legt `_seed_tax_keys` vollständig an). Der Bestand behält überall NULL, nichts
  wird umgedeutet; frische und migrierte Datenbank haben dieselbe Spaltenreihenfolge
- Migration auf 2.4.0 (`VAT_METHOD_SCHEMA`, Schema 2004): hängt `Contacts.VatMethod` (Text, NULL = nicht festgelegt) als letzte Spalte
  an – nach der Sicherung und in EINER Transaktion. Der Bestand behält überall NULL, nichts wird umgedeutet; frische und migrierte
  Datenbank haben dieselbe Spaltenreihenfolge. Die Kontakt-Abfrage (`_CONTACTS_QUERY`) führt sie als Index 34
- Migration auf 2.5.0 (`BUSINESS_ONLY_SCHEMA`, Schema 2005): hängt `Accounts.BusinessOnly` (`INTEGER NOT NULL DEFAULT 1`: 1 = reines
  Geschäftskonto, 0 = gemischt genutzt) als letzte Spalte an – nach der Sicherung und in EINER Transaktion. **Der Bestand bekommt überall 1**
  (alle Konten gelten als reine Geschäftskonten, die Importe laufen wie bisher), nichts wird umgedeutet; frische und migrierte Datenbank
  haben dieselbe Spaltenreihenfolge. `Accounts`-Zeilen werden überall positionsbasiert gelesen (`fetch_accounts`/`get_account_by_id`:
  `IsCash` Index 6, `SKRAccount` Index 7) – die neue Spalte hängt hinten an (Index 8), nichts verschiebt sich. Geschrieben wird nur 0 oder
  1 (`insert_account(business_only=True)`, `update_account(business_only=None)`: `None` = unverändert; sonst `ValueError`), gelesen
  streng (`is_business_only`, `account_allows_selection`). Gepflegt im Formular Stammdaten → Bankkonten (Kästchen „Reines Geschäftskonto“,
  Standard angehakt; bei der Kasse nicht): Ein nicht angehaktes Kästchen schickt nichts, deshalb sagt das versteckte Feld
  `business_only_field=1`, dass das Formular das Kennzeichen führt – nur dann liest der Handler es (genau `1` = angehakt), sonst
  bleibt der Wert. Es ändert keine Buchung, keine Auswertung und keinen Export, nur die Oberfläche des Kontoauszug-Imports
- Migration auf 2.6.0 (`DOCUMENT_READINGS_SCHEMA`, Schema 2006): legt nur die Tabelle `DocumentReadings` (siehe *Belegerkennung*) und den Index
  `idx_documentreadings_gross` auf `GrossMinor` an – nach der Sicherung und in der Transaktion der Umstellung (`_ensure_document_readings_schema`, ohne
  eigenen Schritt in `_run_migrations`; eine frische Datenbank legt sie beim Anlegen an). Keine vorhandene Tabelle ändert sich, der Bestand bekommt keine
  Lesungen und nichts wird umgedeutet: Ein Beleg mit Datei gilt als ungelesen, bis er gelesen wird
- Migration auf 2.7.0 (`MIXED_TAX_SCHEMA`, Schema 2007): hängt `Invoices.MixedTaxRates` (ganze Zahl; NULL = ein Satz für den ganzen Beleg, 1 = Satz je
  Position – siehe *Mehrere Steuersätze je Beleg*) als letzte Spalte an (`InvoiceCol.MIXED_TAX_RATES`, Index 55) – nach der Sicherung und in EINER
  Transaktion. **Der Bestand behält überall NULL** und wird weiter allein nach `Invoices.TaxRate` gelesen, nichts wird umgedeutet (ob alte Belege
  Positionssätze tragen, die vom Kopf abweichen, ist unbekannt – eine Ableitung aus den Positionen buchte sie plötzlich als gemischt); frische und
  migrierte Datenbank haben dieselbe Spaltenreihenfolge. `tests/test_mixed_tax_schema_2007.py`
- Migration auf 2.8.0 (`TAX_RATES_SCHEMA`, Schema 2008): legt `TaxRates` an (`ID, Label, RateBp, Kind, ExemptReason, RevenueCoaId, VatCoaId, InputTaxCoaId, Active, SortOrder,
  CreatedAt`; ohne CHECK-Regeln – was ein Eintrag tragen darf, prüft `tax_groups.check_tax_entry`) und hängt `InvoiceItems` `TaxRateBp` (Index 11), `TaxExemptReason` (12) und
  `RevenueCoaId` (13) an, dazu der Index `idx_invoiceitems_invoice` – nach der Sicherung und in EINER Transaktion (`_ensure_tax_rates_schema` und der Schritt in `_run_migrations`).
  **Der Bestand behält überall NULL** (keine Momentaufnahme: Der Satz steht allein in `TaxRate`), nichts wird abgeleitet; frische und migrierte Datenbank sind gleich (auch ohne
  Fremdschlüssel an `InvoiceItems.RevenueCoaId`). Die Vorbelegung (19 % und 7 %) entsteht genau einmal beim Anlegen der Tabelle – oder beim nächsten Versuch, wenn der erste nach dem
  Anlegen abbrach und die Datenbank noch auf dem alten Stand ist. `tests/test_tax_rates_schema_2008.py`, `tests/entry_form_world.to_state_2007`
- Migration auf 2.9.0 (`SKONTO_SCHEMA`, Schema 2009): hängt `InvoicePayments.SkontoAmount` (ganze Zahl in Minor Units, NULL = kein Skonto) als letzte
  Spalte an – nach der Sicherung und in der Transaktion der Umstellung. **Der Bestand behält überall NULL**, nichts wird umgedeutet; frische und
  migrierte Datenbank haben dieselbe Spaltenreihenfolge. `tests/test_skonto_schema_2009.py`, `tests/entry_form_world.to_state_2008`
