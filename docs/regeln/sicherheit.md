# HTTP-Zugriffsschutz, Eingaben und Ausgaben

## Türsteher und Routen

- `_gate()` in `server/app.py`: schreibende Anfragen nur von eigenen Seiten (`request_is_same_origin`: `Sec-Fetch-Site`, sonst `Origin` gegen `Host`/`X-Forwarded-Host`) – immer, zusätzlich zum CSRF-Token.
- Ohne Login (Ersteinrichtung, automatische Anmeldung) nur unter vertrauenswürdigen Hostnamen (`host_is_trusted`, gegen DNS-Rebinding); weitere über `UBUCHHALTUNG_ALLOWED_HOSTS`.
- **Aktionen mit Seiteneffekt nie per GET** (GET → 405): Pfad in `_ACTION_PATHS`, Logik in `_post_action_routes`, im Frontend `appConfirmPost()`/`appPost()` statt Link. Mit eigener Antwort statt Weiterleitung:
  - Dokumente erzeugen: `_GENERATE_PATHS`, JSON, CSRF-Token als Header (`fetch(url, {method: 'POST'})`).
  - `POST /logout` neben `/login` in `_auth_routes_*`.
  - JSON-Routen in `_JSON_PATHS` + `_POST_ONLY_JSON_PATHS` (z. B. `/invoice/cancel`, `/invoice/reissue`, `/bookings/link-document`, `/bookings/unlink-document`, `/invoice/rebuild-payments` – auch der Probelauf ist ein POST).
  - `/wiso/fdb-upload` in `_UPLOAD_PATHS`: roher Body, JSON-Antwort, CSRF-Token als Header per `XMLHttpRequest`.
- `send_error` (`server/app.py`) trägt immer Statuszeile und Schutz-Header, auch bei kaputter Anfragezeile.

## Anmeldung und Einrichtung

- **Automatische Anmeldung** (`auto_login` in `config.json`, `auth.auto_login_user()`): Administrator ohne Passwort, Anmeldeseite und andere Benutzer abgeschaltet. Keine Sitzung (CSRF-Token an `_csrf_key()` gebunden), Hostnamen-Prüfung wie ohne Login. Ausschalten setzt ein Passwort.
- **Ersteinrichtung** (`handle_setup_start`): legt den ersten Administrator an, trägt `mode: multi` in `config.json` ein; danach ändert nichts im Browser diese Einstellung.
  - Ohne Administrator ist nur `/setup`, `/setup/start` erreichbar (alles andere leitet dorthin); ohne Sitzung kein CSRF-Token – es schützen Herkunfts- und Hostnamen-Prüfung.
  - Maßgeblich ist `auth.admin_exists()`, nicht `config.json`: Fehlt die Datei oder ist sie unlesbar, obwohl es einen Administrator gibt, antwortet der Türsteher mit 503 + Hinweis statt mit der Ersteinrichtung – nie aufweichen.
  - `config.json` atomar schreiben (`userctx._write_config`).
- Nur die Ablage `data/users/<Name>/`; `userctx.get_mode()` kennt nur `'multi'` und `None`, anderes gilt als nicht eingerichtet.
- **Umzug von Altbeständen** (`layout.migrate()`, beim Start vor allem anderen, `run_server()` prüft noch einmal): verschiebt 1.x-Ablage (Daten direkt in `data/`) nach `data/users/admin/`, passt Dateipfade an, schaltet automatische Anmeldung ein; nichts wird gelöscht/überschrieben. Altbestand wird an Rohwerten aus `userctx._read_config()` erkannt, nicht über `get_mode()`.
- **Anmeldung** nur über `auth.attempt_login` (`handlers.handle_login` → `LoginOutcome`), nie direkt `authenticate`.
  - Fünf Fehlversuche in Folge: Name (ohne Groß-/Kleinschreibung) 30 s gesperrt, je weiterem doppelt, höchstens 900 s (429 + `Retry-After`); während der Sperre wird nichts geprüft, gezählt oder verlängert, auch nicht mit richtigem Passwort. Gezählt wird VOR dem Hash.
  - Unbekannte Namen verhalten sich wie vorhandene (eigene Tabelle, 10000 Einträge; Einträge vorhandener Benutzer nie verdrängt). Alles im Speicher.
  - Höchstens zwei scrypt-Läufe gleichzeitig (`_HASH_SLOTS`; nach 10 s Warten 503).
  - Server-Log nennt bei Sperre die Adresse, nie den Namen (dort landet gelegentlich ein Passwort).
  - Passwort mindestens 8 Zeichen. `set_password` beendet die Sitzungen des Benutzers (außer `keep_session`), Ein-/Ausschalten der automatischen Anmeldung alle (`delete_all_sessions`). `conftest` leert die Drosselung je Test (`reset_login_throttle`).
- **`Secure`-Cookie** bei eigenem TLS oder `X-Forwarded-Proto: https` (`forwarded_https`, erster Wert); der Kopf steuert nur dieses Flag.

## SQL-Konsole (`handle_execute_sql`) – Sperren nicht aufweichen

- Nur Administratoren (`auth.sql_console_allowed`); per SQLite-Authorizer auf die eigene Datenbank beschränkt – kein `ATTACH`/`DETACH`/`VACUUM`.
- PRAGMAs nur per Positivliste: mit Argument `_CONSOLE_PRAGMAS_WITH_ARG`, ohne Argument nur rein lesende `_CONSOLE_PRAGMAS_READ` (auch als `pragma_…`-Tabellenfunktion). Alles andere gesperrt, auch `optimize`, `incremental_vacuum`, `wal_checkpoint` ohne Argument.
- Gesperrte Funktionen: `fts3_tokenizer` (springt eine übergebene Speicheradresse an), `load_extension`.
- Je Anfrage: 60 s für alle Befehle zusammen (dann Abbruch + Rollback), Werte 2 MB, Spalten 250, Ausgabe 200000 Zeilen bzw. 20 MB (dann gekürzt).
- **Lesemodus** (Standard; ohne Formularfeld `write` mit genau `1`): `PRAGMA query_only = ON` + Authorizer, der nur Abfragen durchlässt (`_CONSOLE_READ_ACTIONS`); jeder Befehl für sich, Fehler stoppt die folgenden nicht.
- **Schreibmodus** (`write=1`; Seite schickt es nur mit Haken „Schreiben erlauben“ und nach Rückfrage, Haken fällt nach jeder Anfrage):
  - Fremdschlüssel an, Datei wächst höchstens 1 GB (`PRAGMA max_page_count`).
  - **Alle Befehle in EINER Transaktion** (`BEGIN IMMEDIATE` durch den Handler): erster Fehler, Abbruch (Zeit, Platte) oder scheiterndes COMMIT nimmt alles zurück (`rolled_back`, „Nichts wurde geändert …“).
  - Gesperrt: BEGIN, COMMIT, ROLLBACK, SAVEPOINT, RELEASE, `PRAGMA foreign_keys` mit Wert.
  - Trennen mit `sqlite3.complete_statement` (`_console_statements`). Eingabe vor Ausführung begrenzt: 10 MB, 200000 Befehle, je Befehl 2000 geprüfte Semikolons, Budget für erneutes Lesen.
  - Ausgabe bekommt jeder Befehl mit Spalten (auch WITH, PRAGMA, EXPLAIN, RETURNING). Verbindung ohne Statement-Cache (sonst fragt SQLite den Authorizer nicht erneut).
  - Nach COMMIT gehen schreibende Befehle ins SQL-Protokoll (`log_sql`, „SQL-Konsole (<Benutzer>)“, ohne Kommentare, `_console_log_text`); scheitert das: `warnings`.
  - **Eine Anfrage zur Zeit** je Prozess (`_console_lock`, zweite bekommt sofort eine Meldung); Fehlerliste höchstens 100; Abbruch, wenn der freie Platz im Daten-/Temp-Ordner seit Beginn um mehr als 1 GB gesunken ist oder gesunken ist und unter 256 MB liegt.
  - Seitenmeldungen nur über `textContent` (`sqlStatus`). Vorlage „Zeitbereich löschen“ ist für den Schreibmodus gebaut (kein eigenes BEGIN/COMMIT) und hängt Sätze außerhalb des Zeitraums unter einer Bankbewegung des Zeitraums ab, wie `remove_split_row`.
- Der MCP-Server schickt nie `write`.

## Anfragegrößen und Zeitgrenzen (`server/app.py`)

- Kein Body wird gelesen, bevor seine Länge geprüft ist (`_declared_length`: genau EIN `Content-Length` aus ASCII-Ziffern, kein `Transfer-Encoding`; sonst 400).
- Grenzen: 64 KB ohne angemeldeten Benutzer (`MAX_PREAUTH_BODY`: `/login`, `/logout`, Ersteinrichtung), 10 MB Formulare/JSON (`MAX_FORM_BODY`), 200 MB Uploads (`MAX_UPLOAD_BODY`: `/upload_receipts`, `/receipts/upload`, `/wiso/import`), Logos 5 MB; darüber 413, ungelesen, Verbindung zu (`_refuse_body`). WISO-Datenbank 2 GB je Datei (`wiso_upload.MAX_WISO_DB_BODY`, `/wiso/fdb-upload`): nie im Ganzen gelesen, in Stücken auf die Platte, nur wenn danach 64 MB frei bleiben (sonst 507).
- Neue Routen lesen den Body über `_read_body(limit)` bzw. prüfen vorher mit `_check_body_size` – nie `rfile.read(int(Content-Length))`. `_drain_body` liest höchstens 1 MB weg, sonst schließt es.
- **Zeitgrenze je Verbindung:** `CONNECTION_TIMEOUT` = 120 s ohne Lesen/Senden (`SimpleWebServer.timeout`, je Schritt). Antworten in Stücken von `SEND_CHUNK` = 256 KiB (`_SteppedWriter`); nach einem Fehlschlag wirft jeder weitere Schreibzugriff sofort. Body, der nicht endet: 408 + Verbindung zu (`_read_body`); liest ein Handler selbst, schließt `_TimedReader`. Die Bearbeitung hat keine Zeitgrenze (SQL-Konsole: eigene 60 s).
  - **Nicht abschalten:** Die Lesesperre des Datenverzeichnisses (`_run_locked`) hinge sonst an hängenden Verbindungen (`tests/test_connection_timeout.py`).
- **Jede Anfrage wird abgeschlossen** (`_settle_body`, nach der Antwort in `handle_one_request`): ungelesener Body-Rest wird nie als nächste Anfrage gelesen. Rest bis `MAX_DRAIN_BODY` wird mit `DRAIN_TIMEOUT` = 2 s weggelesen, sonst (größerer Rest, `Transfer-Encoding`, ungültige Länge, ausbleibender Rest) Verbindung zu. Nicht umgehen (`tests/test_request_body_sync.py`).
- Leerlauf-Verbindung wird still geschlossen; ließ sich eine Antwort nicht zustellen (`wfile.failed`), reichen die breiten `except`-Zweige den Fehler weiter (`if self.wfile.failed: raise`), statt eine 500 zu versuchen.
- **TLS** (`build_server`): Handshake je Verbindung im eigenen Thread (`SimpleWebServer.setup()`, `TLS_HANDSHAKE_TIMEOUT` = 15 s), nie in `accept()` (`do_handshake_on_connect=False`) – nicht zurückdrehen. Gescheiterter Handshake endet still (`tests/test_tls_handshake.py`). `ACCEPT_QUEUE` = 128.

## Eingaben: Adresse, Formular, Fehlerweg

- **IDs aus der Adresse** nur über `_int_param` (Filter: fehlt/ungültig = nicht angegeben), `_id_param` (fehlt/ungültig = −1) und `self._query()`; nie `int(qs[…][0])` oder `split('?')[1]`. Nicht in 64 Bit passend = ungültig. Aktionen ohne gültige ID: 400 (`tests/test_bad_addresses.py`).
- **Zeitraum** (`from`/`to`, Cookie `period`) nur als ISO-Datum (`period.parse_iso_date`/`valid_period`); `resolve_period` verwirft alles andere, `handlers._period_args` lässt einen ungültigen Zeitraum bei Weiterleitungen weg.
- **Formularwerte** (`server/handlers.py`): IDs nur über `_form_id` (positive ASCII-Ganzzahl in 64 Bit; optional `_optional_form_ids`), Ganzzahlen nur über `_form_int(text, minimum, maximum, default)` (Jahr 1900–2999, Prozent 0–100, Nutzungsdauer 1–100, Zählerstand 0–999 999 999), Kommazahlen nur über `_finite_number` – nie `int(post_data…)`/`float(post_data…)`.
- **Buchungs-/Steuerdatum der Buchungsmaske:** nur Kalendertag `JJJJ-MM-TT` (`parse_iso_date`; Steuerdatum darf leer sein), sonst 400 (`BOOKING_DATE_INVALID`/`TAX_DATE_INVALID`) vor dem ersten Schreiben, bei Anlegen wie Ändern (`tests/test_booking_date_strict.py`).
- **Fehlerweg:** Ungültig → Meldung auf dem Fehlerweg der Seite (303 mit `err=`/`error=`/`msg=`, Klartext über `quote(…, safe='')`, angezeigt nur über `script_json` + `appMsg`); bei ungültiger ID und Seiten ohne Fehlerweg 400 mit Klartext. Nichts wird gespeichert. Ein Handler gibt `(Status, Text)` zurück, `server/app.py` antwortet über `_respond_form_result`: ab 400 steht der Text im Body (`text/plain`), nie im `Location`-Kopf.
- **Nie Exception-Text in einer Antwort:** fester Satz, voller Fehler ins Server-Log (`traceback.print_exc()`). Klartext nur aus eigenen Ablehnungsklassen (`_Refused`, `DeleteRefused`, `ContactRefused`, `BackupRefused` …). Doppelte Kunden-/Kontaktnummer/Kürzel wirft `ContactRefused` (Rollback, Meldung auf der Kontaktseite). `tests/test_form_error_paths.py` hält statisch fest: `handlers.py` hat kein `int(post_data…)`/`float(post_data…)`, keinen 5xx mit Ausnahmetext, keine 3xx mit Ausnahmetext aus breitem `except`.
- **Meldungen auf *Daten* (`msg=`):** Sicherung, Wiederherstellung, DATEV-Export, WISO-Import geben nur den Klartext einer `BackupRefused` weiter, sonst festen Satz (`BACKUP_FAILED`, `RESTORE_FAILED`, `DATEV_FAILED`, `WISO_IMPORT_FAILED`). Die Seite bekommt `msg` vom Server als JSON im Skriptblock `miscMsg` (`script_json`, höchstens `MSG_MAX_LENGTH` = 2000), zeigt es nur über `textContent`/`appMsg` (`tests/test_misc_messages.py`).
- **Beleg-Upload** (`server/upload_handler.py`): je Datei nur Klartext eigener Klassen (`CamtError`, `DkbCsvError`, `document_parser.DocumentExists`), sonst `UPLOAD_FAILED`; Ergebnis je Datei als Text, Neuladen nur ohne Warnung (`tests/test_upload_feedback.py`).

## Ausgabe und Maskierung

- **Jeder Wert aus Datenbank oder Anfrage wird beim Einsetzen maskiert:**
  - HTML-Text/-Attribute: `html.escape(str(wert), quote=True)`.
  - `<script>`: nur `pages.script_json`, nie `json.dumps` allein (`</script>` = XSS); in HTML-Attributen (`onclick="…"`) zusätzlich `html.escape`.
  - Adresse im Inline-JS: `urllib.parse.quote(…, safe='')` (ein maskiertes `'` löst der Browser im `onclick` wieder auf).
  - Browser-JS: nie `innerHTML`/Template-String mit Daten, sondern `textContent`, `createElement`, `.value = …`.
- **Richtext** (Einleitung/Schluss der Angebote) nur über Positivliste `richtext.sanitize_richtext` (b, strong, i, em, u, p, br, ul, ol, li, div, span, ohne Attribute, höchstens 50 Ebenen): beim Speichern UND bei jeder Ausgabe (Formular, PDF), auch Altbestand.
- **Header:** `send_header` entfernt CR/LF/NUL und ersetzt Zeichen außerhalb Latin-1 durch `?` (letzte Sicherung gegen Header-Injection). Jede Antwort trägt `X-Content-Type-Options: nosniff` und `X-Frame-Options: DENY` (`end_headers`); die App rahmt sich nicht selbst ein.
- **Cache-Control:** genau einer je Antwort – der des Handlers (`serve_static_file`), sonst `no-store` für `/backup/download`, `/receipts/file` und die Suchen, `no-cache` für alles andere. Entschieden an der Route, nie an der Query, keine Endungs-Regel (`tests/test_cache_headers.py`).

## Logos – nicht aufweichen

- Nur Bilddateien der Positivliste `export/pdf_core.LOGO_CONTENT_TYPES` (PNG, JPG, GIF, WEBP – kein SVG), an vier Stellen: Upload (`handle_logo_upload`: Endung UND Kennbytes, höchstens 5 MB, an `Content-Length` vor dem Lesen), Auslieferung (`/data/logos/`, `/seed_data/private/`: sonst 404), PDF-Einbettung (`resolve_logo_path`), Hinweis im Kontaktformular (`logo_on_server`).
- `resolve_logo_path` nimmt vom gespeicherten Wert nur den Dateinamen und sucht ihn im `logos`-Verzeichnis des Benutzers (`realpath`; daneben nur `seed_data/private`): ein frei eingetragener Serverpfad ergibt kein Logo.

## E-Mail-Versand (`email_sender.py`)

- Immer verschlüsselt mit Zertifikatsprüfung (`ssl.create_default_context()`; Port 465 `SMTP_SSL`, sonst STARTTLS – ohne STARTTLS-Angebot keine Anmeldung). Kein Abschalt-Schalter; eigenes CA-Zertifikat über `SSL_CERT_FILE` (`DEPLOYMENT.md`).
- Nutzermeldungen nennen nie Exception-Text oder Serverpfade (voller Fehler im Server-Log). Namen über `formataddr`, CR/LF wird abgelehnt.
