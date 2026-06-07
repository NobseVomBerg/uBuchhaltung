---
name: db-navigate
description: Navigate the db/ package efficiently — pick the right domain module, then read or grep it instead of scanning one giant file
user-invocable: false
---

Die Datenbankschicht ist seit dem Split ein **Package `db/`** mit Domänen-Mixins
(früher ein einzelnes ~5200-Zeilen-`db.py`). Die `Database`-Klasse wird in
`db/__init__.py` aus allen Mixins komponiert; `from db import Database` /
`from db import coa_id` funktioniert unverändert.

**Strategie: erst das richtige Modul wählen, dann gezielt lesen.** Die meisten
Module sind 100–600 Zeilen — ein ganzes Domänenmodul zu lesen gibt vollen
Kontext bei moderatem Token-Einsatz. NIE alle Module gleichzeitig laden.

> Hinweis (Windows): das **Grep-Tool** statt bash-`grep` nutzen (plattformunabhängig).

## Modul-Landkarte (`db/`)

| Modul | Inhalt |
|-------|--------|
| `core.py` | `_CoreMixin`: Verbindung, Init-Guard, `_minor_opt`/`_euro_row` (Geld-Grenze), `_log_sql`, `get_table_statistics`, `export_to_sql`; freie Fn `coa_id`; Klassen-Attribute (`_initialized_dbs`, `_init_lock`, `_CONTACTS_QUERY`) |
| `schema.py` | **alle CREATE TABLE** (`initialize_database`), `_create_extended_schema`, `ensure_kasse_exists` |
| `seed.py` | Seed-/Testdaten, SKR-Konten, Steuerschlüssel, `load_test_seed_data`, `is_first_run` |
| `bookings.py` | Buchungen, Buchungsgruppen, Booking↔Document, `fetch_bookings_grouped` |
| `matching.py` | Bank↔Entry-Auto-Linking (`link_bank_to_entries`), `find_unlinked_*` |
| `wiso_import.py` | WISO-CSV-Import (`import_wiso_csv`, `_import_wiso_*`) |
| `accounts.py` | Bankkonten + Kontenrahmen/SKR (`*_chart_of_accounts`, `coa_id_*`) + DATEV-Helfer |
| `assets.py` | Anlagen, Kategorien, AfA-Plan/-Buchung |
| `contacts.py` | Kontakte (normalisiert), Adressen, Personen/Firmen |
| `invoices.py` | Rechnungen, Positionen, Zahlungen, überfällig/fällig |
| `numbering.py` | Nummernkreise (Rechnungsnummern etc.) |
| `articles.py` | Artikel-Stammdaten |
| `receipts.py` / `worktimes.py` | Belege bzw. Arbeitszeiten |
| `reporting.py` | Dashboard-Kennzahlen + EÜR |

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
`Read("db/invoices.py")` (oft komplett sinnvoll, da klein).

**Fremdschlüssel / Joins:**
```bash
grep -rn "FOREIGN KEY\|REFERENCES\|JOIN" db/
```

## Token-Kalkulation

| Aktion | Tokens |
|--------|--------|
| Altes db.py komplett (5200 Z.) | ~15k |
| Domänenmodul komplett (~300 Z.) | ~1–2k |
| `grep -rn def … db/` + 60 Zeilen | ~800 |

Modul wählen → lesen/greppen. Nie das ganze Package blind laden.
