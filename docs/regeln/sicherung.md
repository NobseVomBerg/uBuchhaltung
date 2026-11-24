# Sicherung, Wiederherstellung, Seite „Daten“ und About

## Wiederherstellen (`server/backup.py`)

- Ablauf: Archiv auflisten (`-t7z`, keine Links, keine absoluten/`..`-Pfade) → nach `backup/restore_*` entpacken → prüfen (keine Symlinks, Datenbank lesbar, nicht neuer als das Programm) → erst dann ersetzen.
- Alter Stand nach `backup/replaced_*`, erst nach Erfolg entfernt (`replaced_*` nie automatisch aufräumen). Über Dateisystemgrenzen kopieren statt umbenennen; `buch.db` zuletzt;
  „vorher löschen“ mit Nur-Datenbank-Sicherung abgelehnt. **Diese Reihenfolgen nie umdrehen.**
- Sicherung und Wiederherstellung sperren sich je Datenverzeichnis (`_exclusive`). Nie gesichert/gelöscht/überschrieben: `backup/`, `users/`, `tests/`, `upload_tmp/` (`_EXCLUDED_DIRS`, EINE Liste für alle drei Wege).
- **Meldungen:** Ablehnung = `BackupRefused` (`RuntimeError`; unbekanntes Archiv/Link als Archiv: `BackupNotFound`, zugleich `FileNotFoundError`) mit Klartext – nie Serverpfad, 7-Zip-Ausgabe oder Systemausnahme (nur Server-Log).
  Scheitert `create_backup`, wird das Teilarchiv entfernt (der Name war frei). `list_backups` nennt nur reguläre Dateien, keine Ordner oder Links;
  `_next_archive_path` vergleicht ohne Groß-/Kleinschreibung. Scheitert beim Überschreiben die Datenbank: `OVERLAY_DB_FAILED`; bleibt eine Begleitdatei liegen: `OVERLAY_SIDECARS_LEFT`.
- **Wiederherstellen läuft allein** (`server/dirlock.py`):
  - Jede Anfrage eines angemeldeten Benutzers läuft unter der Lesesperre seines Datenverzeichnisses (`SimpleWebServer._run_locked`; `do_GET`/`do_POST` bestehen nur aus `_gate()` und `_run_locked` – keinen Zweig daran vorbei bauen).
    Ausgenommen `_LOCK_FREE_ROUTES`: `/backup/download`, `/buch.css`, `/favicon.ico` (nur GET).
  - `handle_backup_restore` nimmt die ausschließliche Sperre: neue Anfragen sofort 503 mit `Retry-After` (JSON auf `_JSON_ANSWER_ROUTES`); auf laufende wartet es `RESTORE_WAIT_SECONDS` = 30 s, sonst Ablehnung, nichts geändert.
  - Archivprüfung VOR der ausschließlichen Sperre (`backup.has_archive`).
  - Demo-Modus: `DEMO_WAIT_SECONDS` = 2 s, danach `DEMO_COOLDOWN_SECONDS` = 60 s Ruhe – nicht verlängern bzw. streichen.
  - Sperrreihenfolge: `dirlock` außen, `_exclusive` innen – nie umdrehen. Grenze: gilt nur in einem Prozess (nicht: zweiter Serverprozess, Handzugriffe, Umzug beim Start). `tests/test_dirlock.py`

## Sicherungen herunterladen (`GET /backup/download?file=<Name>`)

- Angeboten wird nur, was `backup.downloadable_backups` nennt (Positivliste): Archive und Kopien vor einer Programm-Umstellung (`<Datum>_vor_<Version>[_n]_buch.db`, nur herunterladbar); reguläre Dateien direkt in `backup/`;
  nie `restore_*`/`replaced_*`, `.part`/`.tmp`, Verstecktes, Ordner, Links, Pfadanteile. Alles andere: dieselbe 404 ohne Pfad.
- Öffnen mit `O_NOFOLLOW` + `realpath`-Prüfung, kurz unter `_exclusive` (wartet `DOWNLOAD_WAIT_SECONDS` = 5 s, sonst 409). Geliefert **ohne Sperre in Stücken** (`copy_to` je 1 MiB, `SEND_CHUNK`), nie ganz im Speicher;
  Zeitgrenze je Stück `DOWNLOAD_SEND_TIMEOUT` = 120 s; `Cache-Control: no-store`. `Range` überhört, kein `HEAD`; Fehler nach den Köpfen schließt die Verbindung. Ein Dateiname, der sich nicht als UTF-8 schreiben lässt, wird weder gelistet noch geliefert.
- Rein lesend, kein CSRF-Token, aber Herkunftsprüfung (`request_is_same_origin`, fremde `Sec-Fetch-Site` → 403); steht **vor `_open_db()`**. Jeder angemeldete Benutzer, nur eigener Ordner. Nicht aufweichen. `tests/test_backup_download.py`

## Seite „Daten“ und About

- `/miscellaneous` („Daten“): Import, Export, Sicherung, Wiederherstellung, Prüfung der Zahlungszuordnungen. *About*: Datenbank-Übersicht (`Database.get_table_statistics`, Tabellennamen als Bezeichner in Anführungszeichen);
  SQL-Konsole und DB-Export nur für `auth.sql_console_allowed` (`PageAbout(db, query)`).
- **DB-Export** `POST /db_export`: prüft das Recht selbst (403 `DB_EXPORT_REFUSED`, nichts geschrieben), leitet auf `/about?export=ok&tables=N&rows=M` bzw. `export=error`; Zahlen streng gelesen (`pages.export_message`, nur Ziffern).
  `Database.export_to_sql`: Namen als Bezeichner in Anführungszeichen, Werte als Literale (`_sql_identifier`, `_sql_literal`, Binär `X'…'`) – ein Tabellenname wird nie ein eigener Befehl.
- **Sicherungen löschen** `POST /backup/delete` (`_ACTION_PATHS`, Feld `file`, Frontend `appConfirmPost`): nur, was `downloadable_backups` nennt (`backup.delete_backup`: reiner Dateiname, reguläre Datei in `backup/`, `realpath`, nie `restore_*`/`replaced_*`,
  Teildateien, Ordner, Links; sonst dieselbe Meldung wie beim Download); unter `_exclusive` (läuft Sicherung/Wiederherstellung: abgelehnt ohne Warten); Demo 403 (`BACKUP_DELETE_DEMO_REFUSED`).
  Meldung nennt nur den Namen aus der Liste, nie Pfad/Ausnahmetext; der Vorspann steht fest im Skript, `msg` trägt nur Name bzw. Grund.
- Nicht aufweichen. `tests/test_data_page_cleanup.py`
