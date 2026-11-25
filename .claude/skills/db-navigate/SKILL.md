---
name: db-navigate
description: Navigate the db/ package efficiently — pick the right domain module, then read or grep it instead of scanning one giant file
user-invocable: false
---

Die Datenbankschicht ist ein **Package `db/`** mit Domänen-Mixins. Die `Database`-Klasse wird in
`db/__init__.py` aus allen Mixins komponiert; `from db import Database` / `from db import coa_id` ist der Zugang.

**Strategie: erst das richtige Modul wählen, dann gezielt lesen.** Ein Domänenmodul ganz zu lesen gibt vollen Kontext
bei moderatem Token-Einsatz; die großen (`invoice_payments.py`, `matching.py`, `invoices.py`) per Grep und Zeilenbereich.
NIE alle Module gleichzeitig laden.

> Hinweis (Windows): das **Grep-Tool** statt bash-`grep` nutzen (plattformunabhängig).

## Modul-Landkarte (`db/`)

| Modul | Inhalt |
|-------|--------|
| `core.py` | `_CoreMixin`: Verbindung, Init-Guard, `transaction`, `_minor_opt`/`_euro_row` (Geld-Grenze), `_log_sql`, `get_table_statistics`, `export_to_sql`; freie Fn `coa_id`; Klassen-Attribute (`_initialized_dbs`, `_init_lock`, `_CONTACTS_QUERY`) |
| `schema.py` | **alle CREATE TABLE** (`initialize_database`), `_create_extended_schema`, `_ensure_*_schema` (Tabellen und Indizes in der Transaktion der Umstellung), `ensure_kasse_exists`, Migrationen, `SchemaTooNewError`, `MigrationBackupError` |
| `seed.py` | Seed-/Testdaten, SKR-Konten, Steuerschlüssel, `load_test_seed_data`, `is_first_run`, `missing_standard_accounts`, `add_missing_standard_accounts` |
| `datev_keys.py` | BU-Schlüssel des DATEV-Stapels, rein, ohne Datenbank: `derive_key`, `key_rate`, `parse_key`/`checked_key`, `booking_kind`, `is_receivable_row`, `is_vat_row` |
| `bookings.py` | Buchungen, Buchungsgruppen, Booking↔Document, `fetch_bookings_grouped`, Speichern des Bank-Editors in einer Transaktion (`save_bank_with_rows`); Zusatzfelder einer Bankbewegung (Tabelle `BankTransactionDetails`: `insert_booking(bank_details=…)`, `get_bank_details` → `BankDetails`) und der Bestand der Duplikatprüfung mit Bankreferenz (`bank_stock`, gleiche Länge wie `check_booking_exists`) |
| `matching.py` | Bank↔Entry-Auto-Linking (`link_bank_to_entries`, Richtungsregel `link_effect`), `find_unlinked_*`; Lesart einer Buchungszeile für Export und Summen- und Saldenliste (`ledger_form`, `coa_in_soll`, `soll_side`), Übertragsrechnung (`settle_transfers`, `_settle_stock`) |
| `wiso_import.py` | WISO-Textimport (`import_wiso_csv`, `_import_wiso_original_format`, `_import_wiso_table_format`) |
| `wiso_fdb_import.py` | WISO-Datenbank-Import (Mixin `WisoFdbImportMixin`; Abbildung auf `Bookings`, `Assets`, `AssetDepreciations`; die Datei liest `importers/wiso_fdb`) |
| `accounts.py` | Bankkonten + Kontenrahmen/SKR (`*_chart_of_accounts`, `get_coa_id_by_account_number`, `get_coa_id_to_number_map`) + DATEV-Helfer (`fetch_bookings_range`, `LEDGER_ROWS_WHERE`: das `WHERE` der Zeilen von Export und Summen- und Saldenliste; Steuerdatum und Merkmal „exportiert“ in EINER Transaktion: `mark_datev_export`, `exported_booking_ids`, Tabelle `BookingExports`) |
| `assets.py` | Anlagen, Kategorien, AfA-Plan/-Buchung |
| `contacts.py` | Kontakte (normalisiert), Adressen, Personen/Firmen, Besteuerungsart der eigenen Firma (`vat_method`, `get_vat_method`, `VAT_METHODS`) |
| `invoices.py` | Rechnungen, Positionen, Zahlungen, überfällig/fällig, Forderungsbuchung (`_book_receivable`, `_receivable_removal`), Entwurf löschen, Angebot → Rechnung (merkt dessen Status: `restored_quote_status`); strenge Leser der Spalten 56–58 (`payment_means_code`/`stored_payment_means`, `service_period`/`normalize_service_end`) |
| `invoice_lock.py` | Sperre versendeter Rechnungen (`update_invoice_fields`, `check_changes`, `row_group`: die Stornorechnung ist ganz gesperrt) und Statusübergänge (`set_invoice_status`, `TRANSITIONS` – nach „storniert“ nur bei Brutto ≤ 0 (`NO_POSITIVE`), sonst `cancel_invoice`; Status, Forderung, Zahlungsumzug zur Ersatzrechnung (`_payment_move_context`) und Fassungen in einer Transaktion; das Zurückholen fragt nach, wenn die Forderung schon in einem DATEV-Export stand: `confirm_exported`, `REOPEN_EXPORTED_QUESTION`) |
| `invoice_versions.py` | Berichtigen (`correct_invoice`), Fassungen (Tabelle `InvoiceDocumentVersions`, `_archive_documents`) und Protokoll (Tabelle `InvoiceCorrections`, `get_invoice_protocol`), `was_sent_before`, Hinweis der berichtigten Fassung (`correction_notice`), `is_iso_date` |
| `invoice_storno.py` | Stornorechnung (`cancel_invoice`: Beleg, negierte Positionen, Gegenbuchung der Forderung in einer Transaktion, auch nachträglich für eine von Hand stornierte Rechnung), Ersatzrechnung (`create_replacement_draft`, `find_replacement_invoice`, `find_replaced_invoice`) und Guthaben des Storno-Paares (`get_storno_credits`); `find_storno_invoice`, `find_cancelled_invoice` |
| `invoice_repair.py` | Zuordnung neu aufbauen (`rebuild_invoice_payments`: Probelauf mit Rollback, Prüfsumme, Bestätigung; kein Buchungssatz wird gelöscht oder geändert, `RebuildPlan`, `REBUILD_SKIP_REASONS`) |
| `tax_rates.py` | Steuersätze der Stammdaten (Tabelle `TaxRates`): `TaxEntry`, `fetch_tax_rates`/`get_tax_rate`, Anlegen/Ändern/Abschalten/Löschen in je einer Transaktion (`TaxRateRefused`; Regeln in `tax_groups.check_tax_entry`), Momentaufnahme für die Position (`entry_snapshot`, `legacy_expressible`), Kontenauswahl (`get_tax_account_choices`), Steuerkonten für die Auswertungen (`select_report_tax_accounts`, `_report_tax_accounts`) |
| `invoice_payments.py` | Zahlungszuordnung, Erstattung, Prüfliste (`find_payment_inconsistencies`), Satzgruppen eines Belegs (`_mixed_groups`), Momentaufnahme eines Steuersatzes an den Positionen (`ITEM_SNAPSHOT_KEYS`, `group_snapshots`, `document_entry`, `valid_rate_bp`, `TAX_ENTRY_CONTRADICTION`), `has_mixed_rates`, `rate_sentence` |
| `numbering.py` | Nummernkreise (Rechnungs- und Angebotsnummern etc.; `allocate_document_number`, `allocate_invoice_number`, `release_invoice_number`, `number_format_error`) |
| `articles.py` | Artikel-Stammdaten |
| `receipts.py` / `worktimes.py` / `trips.py` | Belege, Arbeitszeiten bzw. Fahrtenbuch (`logbook_gaps`) |
| `document_readings.py` | Lesung eines Belegs (Tabelle `DocumentReadings`: aus der Datei gelesene Felder, abgeleitet, nicht im Replay-Log): `save_document_reading`, `get_document_reading`, `document_reading_summaries`, `unread_receipt_ids`/`count_unread_receipts`, `own_identity`; Vorschläge Beleg → Buchung, rein lesend: `receipt_match_candidates` (eine Abfrage mit allen Ausschlüssen, Forderung über `_receivable_sql` aus `matching.py`), `suggest_bookings_for_receipt` (Abfrage + Rangfolge; leer, solange der Beleg an einer Buchung hängt: `receipt_is_linked`) |
| `receipt_matching.py` | Rangfolge der Buchungsvorschläge zu einem gelesenen Beleg, rein (kein DB-Zugriff, keine Uhr): `candidate_window`, `rank_candidates` → `Suggestion`, `usable_gross`, `normalize_number`, `name_words`; Punkte, Schwellen und Begründungen als Modulkonstanten |
| `payment_matching.py` | Rangfolge der Zahlungsvorschläge Bankbewegung ↔ offene Rechnung, rein: `rank_invoices`, `rank_movements`, `pair_up`, `reason_text`; Nummer im Text mit Zifferngrenzen |
| `payment_suggestions.py` | Lesende Abfragen dazu (Mixin): `suggest_invoice_payments`, `suggest_invoices_for_booking`, `suggest_bookings_for_invoice`; Skonto einer Rechnung für die Seiten: `skonto_offer`, `skonto_limits` |
| `reporting.py` | Dashboard-Kennzahlen + EÜR (`get_euer_data`, mit `only_asset_transfers=True` nur die Umbuchungen zwischen Anlage- und Erfolgskonto ohne Geldfluss – Sektion 3 und die Umbuchungen dieser Art unter einer Bankbewegung; `euer_section`: Einnahme, Ausgabe oder „nicht im Ergebnis“ – eine Regel für die EÜR-Seite und die BWA; `get_bwa`; `get_susa`: Summen- und Saldenliste, SQL-gruppiert, rein lesend; `get_liquidity`: Liquiditätsrechnung aus den Zeilen der EÜR, den Umbuchungen ohne Geldfluss, der BWA und den Töpfen des Dashboards, dazu `_liquidity_unbooked`) |
| `bwa.py` | Betriebswirtschaftliche Auswertung: Gruppentabelle (`RANGES`, `EXCEPTIONS`, `group_of`), Zeiträume, Zeilenaufbau aus `get_euer_data`, Prozent- und Formatregeln (rein, ohne Datenbank) |
| `liquidity.py` | Liquiditätsrechnung: Bereiche als Daten (`DISPOSAL_RANGES`, `FINANCING_RANGE`, `WITHDRAWAL_RANGE`, `DEPOSIT_RANGE`, `ASSET_LIMIT`), Aufbau der Überleitung vom Ergebnis der BWA zur Veränderung der flüssigen Mittel (`build_liquidity` mit den Zeilen der EÜR und der Umbuchungen ohne Geldfluss, `CashFigures`, „Nicht zugeordnet“ als Differenz), Spalten, Sichtbarkeit, Texte (rein, ohne Datenbank) |
| `euer.py` | EÜR-Aufbereitung für den Kasten des Dashboards und die Seite `/euer` (`euer_statement`: Abschnitte nach `euer_section`, Summen in Minor Units; `range_label`, `euer_filename`, Texte; rein, ohne Datenbank) |
| `susa.py` | Summen- und Saldenliste: Kontenklassen und Vortrag (`GROUPS`, `group_key`, `is_carried`), Buchungsregel je Satz mit Steuer (`entry_postings`, `tax_side`, `TAX_ACCOUNTS`), Gruppe buchen (`book_group`, `Tally`), Bericht (`build_susa`), Anzeige und Texte (rein, ohne Datenbank) |

## Gezielt navigieren

**Methode finden (Modul unbekannt):**
```bash
grep -rn "def <methodname>" db/
```

**Tabellen-Schema (alle CREATE TABLE):** → immer `db/schema.py`
```bash
grep -n "CREATE TABLE" db/schema.py
```

**Eine Domäne ganz verstehen:** das passende Modul lesen, z. B.
`Read("db/articles.py")` (kleine Module ganz, große gezielt).

**Fremdschlüssel / Joins:**
```bash
grep -rn "FOREIGN KEY\|REFERENCES\|JOIN" db/
```

Modul wählen → lesen/greppen. Nie das ganze Package blind laden.
