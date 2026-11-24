# Sicherung, Wiederherstellung, Seite „Daten“ und About

## Sicherung und Wiederherstellung

- **Wiederherstellen** (`server/backup.py`): Ein 7-Zip-Archiv wird vor dem Entpacken
  aufgelistet (`-t7z`, keine Links, keine absoluten oder `..`-Pfade), dann nach
  `backup/restore_*` entpackt, geprüft (keine Symlinks, Datenbank lesbar und nicht neuer
  als das Programm) und erst danach ersetzt. Der vorherige Stand wird nach
  `backup/replaced_*` verschoben, nicht vorab gelöscht, und erst nach Erfolg entfernt
  (`replaced_*` nie automatisch aufräumen). Über Dateisystemgrenzen wird kopiert statt
  umbenannt; beim Überschreiben kommt `buch.db` zuletzt; „vorher löschen“ mit einer
  Nur-Datenbank-Sicherung wird abgelehnt. Sicherung und Wiederherstellung sperren sich
  je Datenverzeichnis (`_exclusive`). Diese Reihenfolgen nie umdrehen. Nie gesichert, gelöscht oder überschrieben werden
  `backup/`, `users/`, `tests/` und `upload_tmp/` (`_EXCLUDED_DIRS`, eine Liste für alle drei Wege). **Meldungen:** Eine
  Ablehnung des Moduls ist `BackupRefused` (eine `RuntimeError`; ein unbekanntes Archiv oder ein Link als Archiv
  `BackupNotFound`, zugleich `FileNotFoundError`) mit Klartext – nie ein Serverpfad, nie die Ausgabe von 7-Zip, nie der Text
  einer Systemausnahme: Die stehen im Server-Log (`from e`, `print`). Scheitert `create_backup`, wird das Teilarchiv entfernt
  (der Name war frei, es ist nie eine fremde Datei); `list_backups` nennt nur reguläre Dateien, keine Ordner oder Links;
  `_next_archive_path` vergleicht ohne Groß-/Kleinschreibung.
  Scheitert beim Überschreiben erst der Schritt der Datenbank, sagt die Meldung den Stand (`OVERLAY_DB_FAILED`; bleibt eine
  Begleitdatei der alten Datenbank liegen: `OVERLAY_SIDECARS_LEFT` – die übrigen werden trotzdem entfernt). **Wiederherstellen läuft
  allein (seit 2.5.6, `server/dirlock.py`):** Jede Anfrage eines angemeldeten Benutzers läuft unter der Lesesperre seines
  Datenverzeichnisses (`SimpleWebServer._run_locked`; `do_GET`/`do_POST` bestehen nur aus `_gate()` und `_run_locked` – keinen Zweig
  daran vorbei bauen; ausgenommen `_LOCK_FREE_ROUTES`: `/backup/download`, `/buch.css`, `/favicon.ico`, nur GET), `handle_backup_restore`
  nimmt die ausschließliche: Neue Anfragen bekommen sofort 503 mit `Retry-After` (als JSON auf den Routen, die ihre Seiten als JSON
  lesen: `_JSON_ANSWER_ROUTES`), auf laufende wartet es höchstens `RESTORE_WAIT_SECONDS` = 30 s, sonst wird abgelehnt und nichts
  geändert. Ob es das Archiv gibt, prüft der Handler VOR der ausschließlichen Sperre (`backup.has_archive`): Ein Versuch, der nie
  beginnen kann, hält keine Anfrage auf. Im Demo-Modus (alle Besucher teilen ein Verzeichnis) wartet das Wiederherstellen nur
  `DEMO_WAIT_SECONDS` = 2 s und ruht nach einer Ablehnung `DEMO_COOLDOWN_SECONDS` = 60 s (neue Versuche werden sofort abgelehnt, ohne
  Anfragen aufzuhalten) – nicht verlängern bzw. streichen. Reihenfolge der Sperren: `dirlock` außen, `_exclusive` innen – nie umdrehen. Die Sperre gilt in einem Prozess; nicht
  erfasst sind ein zweiter Serverprozess auf demselben Datenordner, Zugriffe von Hand und der Umzug beim Start
  (`tests/test_dirlock.py`)
- **Sicherungen herunterladen (seit 2.5.5, `GET /backup/download?file=<Name>`):** Die Seite *Daten* listet die Sicherungen
  des angemeldeten Benutzers (Name, Art, Datum, Größe) mit einem Link. Angeboten wird allein, was `backup.downloadable_backups`
  nennt – eine Positivliste: die Archive und die Kopien vor einer Programm-Umstellung (`<Datum>_vor_<Version>[_n]_buch.db`, nur
  herunterladbar, nicht im Formular der Wiederherstellung), nur reguläre Dateien direkt in `backup/`, nie
  `restore_*`/`replaced_*`, `.part`/`.tmp`, Verstecktes, Ordner, Links oder Namen mit Pfadanteil; jede andere Anfrage ist
  dieselbe 404 ohne Pfad. Geöffnet wird mit `O_NOFOLLOW` und gegen `realpath` geprüft (`backup/` selbst darf ein Link sein),
  kurz unter `_exclusive` (das Öffnen wartet bis `DOWNLOAD_WAIT_SECONDS` = 5 s – zwei Downloads lehnen einander nicht ab –,
  läuft eine Sicherung oder Wiederherstellung länger: 409; Sicherung und Wiederherstellung selbst warten nie); geliefert wird
  **ohne die Sperre in Stücken** (`copy_to` liest je 1 MiB, gesendet wird in Schritten von `SEND_CHUNK`) aus dem offenen
  Deskriptor, nie ganz im Speicher – eine Sicherung kann Gigabyte groß sein. Je Stück gilt eine Zeitgrenze (`DOWNLOAD_SEND_TIMEOUT` = 120 s: Ein Client, der nicht mehr liest, hält
  Verbindung und Datei nicht unbegrenzt), und die Antwort trägt `Cache-Control: no-store`. Ein `Range`-Kopf wird überhört,
  `HEAD` gibt es nicht; ein Fehler nach den Köpfen schließt die Verbindung, statt eine zweite Antwort zu schreiben. Ein
  Dateiname, der sich nicht als UTF-8 schreiben lässt, wird weder gelistet noch geliefert. Die Route liest nur (kein CSRF-Token, wie die übrigen GET-Downloads), prüft aber die Herkunft
  (`request_is_same_origin`: eine fremde `Sec-Fetch-Site` ergibt 403) und steht **vor `_open_db()`**: Sicherungen lassen sich
  auch holen, wenn sich die Datenbank nicht öffnen lässt. Jeder angemeldete Benutzer, nur aus dem eigenen Ordner; im Demo-Modus
  wie Sichern und Wiederherstellen. Nicht aufweichen (`tests/test_backup_download.py`)
- **Seite „Daten“ und About (seit 2.8.3):** Die Seite unter `/miscellaneous` heißt im Menü „Daten“ (die Adresse bleibt) und führt nur noch
  Import, Export, Sicherung, Wiederherstellung und die Prüfung der Zahlungszuordnungen. Die **Datenbank-Übersicht** steht auf *About* unter
  „Projekt in Zahlen“ (für jeden angemeldeten Benutzer; `Database.get_table_statistics` setzt jeden Tabellennamen als Bezeichner in
  Anführungszeichen, eine nicht zählbare Tabelle zeigt `–`). **SQL-Konsole und DB-Export** stehen ganz unten auf *About* – Kästen und Skripte
  nur für die, die die Konsole benutzen dürfen (`auth.sql_console_allowed`; `PageAbout(db, query)`). Der DB-Export (`POST /db_export`) prüft
  dasselbe Recht selbst (403 `DB_EXPORT_REFUSED`, nichts geschrieben) und leitet auf `/about?export=ok&tables=N&rows=M` bzw. `export=error`;
  die Seite liest die Zahlen streng (`pages.export_message`, nur Ziffern) und zeigt einen festen Satz. An den Regeln der Konsole selbst
  (*SQL-Konsole*) ändert sich nichts. **Sicherungen löschen** (`POST /backup/delete`, `_ACTION_PATHS`, Formularfeld `file`; im Frontend
  `appConfirmPost(url, text, fields)` nach Rückfrage): gelöscht wird allein, was `backup.downloadable_backups` nennt (`backup.delete_backup`:
  reiner Dateiname, reguläre Datei direkt in `backup/`, gegen `realpath` geprüft, nie `restore_*`/`replaced_*`, Teildateien, Ordner oder Links
  – alles andere ist dieselbe Meldung wie beim Download, nichts angefasst), unter `_exclusive` (läuft eine Sicherung oder Wiederherstellung:
  abgelehnt, ohne zu warten); im Demo-Modus 403 (`BACKUP_DELETE_DEMO_REFUSED`, die Seite zeigt den Knopf nicht). Die Meldung nennt den Namen
  aus der Liste, nie einen Pfad oder Ausnahmetext; der Vorspann („Sicherung gelöscht: “/„Sicherung nicht gelöscht: “) steht fest im Skript, `msg`
  trägt nur Name bzw. Grund. **Der DB-Export** (`Database.export_to_sql`) schreibt Tabellen- und Spaltennamen als Bezeichner in Anführungszeichen
  und Werte als Literale (`_sql_identifier`, `_sql_literal`: Binärdaten als `X'…'`) – ein Tabellenname wird nie ein eigener Befehl. Nicht
  aufweichen (`tests/test_data_page_cleanup.py`)
