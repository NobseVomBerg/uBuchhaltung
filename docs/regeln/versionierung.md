# Schema-Migrationen und Projektzahlen

## Projektzahlen bei jedem Versionssprung

- **Bei jedem Versionssprung `python tools/project_stats.py` ausführen und `project_stats.json` mit committen.** Sie trägt die Zahlen von „Projekt in Zahlen“ (About) und die Version, aus der sie stammen;
  `tests/test_project_stats.py` verlangt, dass sie zu `APP_VERSION` gehört (`--check` prüft nur das).
- Das Werkzeug zählt im Projektordner (`os.walk`, Standardbibliothek, ohne `git`), sammelt Testfälle mit `pytest --collect-only` (scheitert das: schreibt nichts), schreibt weder Datum noch Pfade.
- About liest nur diese Datei (`server/project_stats.py`, streng: höchstens 64 KB, ganze Zahlen 0 bis 10⁹, Version aus `[0-9A-Za-z.+-]`; jede Abweichung = `None`, Abschnitt fehlt); zur Laufzeit wird nichts gezählt (im Docker-Image fehlt `tests/`).

## Schema-Migrationen (je Stufe)

Alle laufen nach der Sicherung und in EINER Transaktion; frische und migrierte Datenbank haben dieselbe Spaltenreihenfolge; der Bestand wird nie umgedeutet (NULL, wo nicht anders genannt).

- 2002 (`INVOICE_VERSIONS_SCHEMA`): `Invoices` +`InvoiceKind`, `CancelsInvoiceId`, `ReplacesInvoiceId` (ohne Index), `SentAt`, `DocumentVersion` (`InvoiceCol` 50–54; NULL = Fassung 1); neu `InvoiceCorrections`, `InvoiceDocumentVersions`
  (bewusst ohne Fremdschlüssel: überleben das Löschen eines Entwurfs), eindeutiger Index auf `CancelsInvoiceId`.
- 2003 (`DATEV_KEY_SCHEMA`): `Bookings.DatevKey` (Index 23), `CompanyDetails.DatevConsultantNo`/`DatevClientNo`; berichtigt `TaxKeys` (`TAX_KEY_CORRECTIONS`: 100/120/121/122/490; abweichende Zeile überschrieben, fehlende angelegt, richtige unberührt;
  nur wenn die Tabelle Zeilen hat, die leere legt `_seed_tax_keys` an).
- 2004 (`VAT_METHOD_SCHEMA`): `Contacts.VatMethod` (NULL = nicht festgelegt); `_CONTACTS_QUERY` Index 34.
- 2005 (`BUSINESS_ONLY_SCHEMA`): `Accounts.BusinessOnly` (`INTEGER NOT NULL DEFAULT 1`; 1 = reines Geschäftskonto, 0 = gemischt) als Index 8. **Bestand bekommt überall 1.**
  `Accounts` wird positionsbasiert gelesen (`fetch_accounts`/`get_account_by_id`: `IsCash` 6, `SKRAccount` 7) – neue Spalten nur hinten anhängen. Geschrieben nur 0/1 (`insert_account(business_only=True)`, `update_account(business_only=None)` = unverändert, sonst `ValueError`),
  gelesen streng (`is_business_only`, `account_allows_selection`). Formular Bankkonten: Kästchen „Reines Geschäftskonto“ (Standard an, bei Kasse nicht); ein nicht angehaktes schickt nichts – das versteckte Feld `business_only_field=1`
  sagt, dass das Formular es führt, nur dann liest der Handler es (genau `1` = angehakt), sonst bleibt der Wert. Ändert nur die Oberfläche des Kontoauszug-Imports.
- 2006 (`DOCUMENT_READINGS_SCHEMA`): nur `DocumentReadings` + Index `idx_documentreadings_gross` (`_ensure_document_readings_schema`, ohne eigenen Schritt in `_run_migrations`). Beleg mit Datei gilt als ungelesen.
- 2007 (`MIXED_TAX_SCHEMA`): `Invoices.MixedTaxRates` (NULL = ein Satz, 1 = Satz je Position; `InvoiceCol.MIXED_TAX_RATES`, Index 55). **Bestand wird weiter allein nach `Invoices.TaxRate` gelesen – nie aus den Positionen ableiten.** `tests/test_mixed_tax_schema_2007.py`
- 2008 (`TAX_RATES_SCHEMA`): neu `TaxRates` (`ID, Label, RateBp, Kind, ExemptReason, RevenueCoaId, VatCoaId, InputTaxCoaId, Active, SortOrder, CreatedAt`; ohne CHECK – prüft `tax_groups.check_tax_entry`);
  `InvoiceItems` +`TaxRateBp` (11), `TaxExemptReason` (12), `RevenueCoaId` (13, ohne Fremdschlüssel), Index `idx_invoiceitems_invoice` (`_ensure_tax_rates_schema`, Schritt in `_run_migrations`). Bestand ohne Momentaufnahme.
  Vorbelegung (19 % und 7 %) genau einmal beim Anlegen der Tabelle (oder beim nächsten Versuch, wenn der erste nach dem Anlegen abbrach). `tests/test_tax_rates_schema_2008.py`, `tests/entry_form_world.to_state_2007`
- 2009 (`SKONTO_SCHEMA`): `InvoicePayments.SkontoAmount` (Minor Units, NULL = kein Skonto). `tests/test_skonto_schema_2009.py`, `tests/entry_form_world.to_state_2008`
