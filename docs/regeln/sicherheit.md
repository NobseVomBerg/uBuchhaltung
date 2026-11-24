# HTTP-Zugriffsschutz, Eingaben und Ausgaben

## Zugriffsschutz (HTTP)

- Türsteher ist `_gate()` in `server/app.py`: schreibende Anfragen nur von eigenen
  Seiten (`request_is_same_origin`: `Sec-Fetch-Site`, sonst `Origin` gegen `Host`/
  `X-Forwarded-Host`) – immer, zusätzlich zum CSRF-Token
- Ohne Login (Ersteinrichtung, automatische Anmeldung) nur unter vertrauenswürdigen
  Hostnamen (`host_is_trusted`, gegen DNS-Rebinding); weitere Namen über
  `UBUCHHALTUNG_ALLOWED_HOSTS`
- **Aktionen mit Seiteneffekt nie per GET:** Pfad in `_ACTION_PATHS` eintragen,
  Logik in `_post_action_routes`, im Frontend `appConfirmPost()`/`appPost()`
  statt eines Links. Mit eigener Antwort statt Weiterleitung: das Erzeugen von
  Dokumenten (`_GENERATE_PATHS`, JSON, CSRF-Token als Header – im Frontend
  `fetch(url, {method: 'POST'})`, das Footer-JS hängt das Token an), Abmelden
  (`POST /logout` neben `/login` in `_auth_routes_*`) und das Stornieren und Neu-Ausstellen einer Rechnung
  (`/invoice/cancel`, `/invoice/reissue`: JSON-Routen in `_JSON_PATHS`, dazu `_POST_ONLY_JSON_PATHS`), das Verknüpfen von Belegen
  (`/bookings/link-document`, `/bookings/unlink-document`: ebenso), der Neuaufbau einer Zahlungszuordnung
  (`/invoice/rebuild-payments`: ebenso – auch der Probelauf ist ein POST, er nimmt kurz die Schreibsperre) und der Upload der
  WISO-Datenbank (`/wiso/fdb-upload` in `_UPLOAD_PATHS`: roher Body, JSON-Antwort, CSRF-Token als Header per
  `XMLHttpRequest`); GET → jeweils 405
- **Automatische Anmeldung** (`auto_login` in `config.json`, `auth.auto_login_user()`):
  ein Administrator wird ohne Passwort angemeldet, Anmeldeseite und andere Benutzer
  sind so lange abgeschaltet – das ist der Betrieb für eine Person. Es gibt dann
  keine Sitzung (CSRF-Token an `_csrf_key()` gebunden), und die Hostnamen-Prüfung
  greift wie ohne Login. Schalter in der Benutzerverwaltung; Ausschalten setzt ein Passwort
- Die Ersteinrichtung (`handle_setup_start`) legt den ersten Administrator an und
  trägt `mode: multi` in `config.json` ein (= eingerichtet); danach ändert nichts im
  Browser diese Einstellung. Solange es keinen Administrator gibt, ist nur die
  Ersteinrichtung erreichbar (`/setup`, `/setup/start`; alles andere leitet dorthin).
  Ohne Sitzung gibt es dort kein CSRF-Token – es schützen die Herkunfts- und die
  Hostnamen-Prüfung. Maßgeblich ist `auth.admin_exists()`, nicht `config.json`: fehlt
  die Datei oder ist sie unlesbar, obwohl es einen Administrator gibt, antwortet der
  Türsteher mit einem Hinweis (503) statt mit der Ersteinrichtung – nie aufweichen.
  `config.json` wird atomar geschrieben (`userctx._write_config`)
- Es gibt nur die Mehrbenutzer-Ablage `data/users/<Name>/`; der Betrieb für eine Person
  ist die automatische Anmeldung. `userctx.get_mode()` kennt nur `'multi'` (eingerichtet)
  und `None`; jeder andere Wert in `config.json` gilt als nicht eingerichtet
- **Umzug von Altbeständen** (`layout.migrate()`, läuft beim Start vor allem anderen,
  `run_server()` prüft es zur Sicherheit noch einmal): verschiebt eine Ablage aus
  Version 1.x (Daten direkt in `data/`) nach `data/users/admin/`, passt gespeicherte
  Dateipfade an und schaltet die automatische Anmeldung ein; nichts wird gelöscht oder
  überschrieben. Den Altbestand erkennt `layout.py` an den Rohwerten aus
  `userctx._read_config()`, nicht über `get_mode()`
- **SQL-Konsole** (`handle_execute_sql`): nur Administratoren
  (`auth.sql_console_allowed`); per SQLite-Authorizer auf die eigene Datenbank
  beschränkt – kein `ATTACH`/`DETACH`/`VACUUM`. PRAGMAs laufen nur per Positivliste:
  mit Argument die Abfrage-PRAGMAs (`_CONSOLE_PRAGMAS_WITH_ARG`), ohne Argument nur
  rein lesende (`_CONSOLE_PRAGMAS_READ`) – auch als `pragma_…`-Tabellenfunktion, die
  SQLite mit derselben Authorizer-Aktion meldet. Alles andere ist gesperrt, auch
  `optimize`, `incremental_vacuum` und `wal_checkpoint` ohne Argument. Gesperrte
  Funktionen: `fts3_tokenizer` (springt in Debian-/Ubuntu-Builds von SQLite eine
  übergebene Speicheradresse an – Absturz bzw. fremder Code) und `load_extension`.
  Je Anfrage gelten ein Zeitlimit (60 s für alle Befehle zusammen, danach Abbruch und
  Rollback) sowie Größengrenzen für einzelne Werte (2 MB), Spalten (250) und die
  Ausgabe (200000 Zeilen bzw. 20 MB, danach gekürzt). Diese Sperren nicht aufweichen.
  **Zwei Modi (seit 2.3.11):** Ohne das Formularfeld `write` mit genau dem Wert `1` läuft die Konsole **nur lesend** – in zwei
  Schichten: `PRAGMA query_only = ON` und ein Authorizer, der nur Abfragen durchlässt (`_CONSOLE_READ_ACTIONS`: SELECT, Lesen,
  Funktionen, rekursive Abfragen, PRAGMAs der Positivlisten); jeder Befehl läuft für sich, ein Fehler stoppt die folgenden nicht.
  **Schreibmodus** (`write=1`; die Seite schickt es nur mit dem Haken „Schreiben erlauben“ und nach einer Rückfrage, der Haken
  fällt nach jeder Anfrage): Fremdschlüssel sind an, die Datei darf um höchstens 1 GB wachsen (`PRAGMA max_page_count`), und
  **alle Befehle laufen in EINER Transaktion** (`BEGIN IMMEDIATE` durch den Handler): Der erste Fehler, ein Abbruch (Zeit,
  Platte) oder ein scheiterndes COMMIT nimmt alles zurück (`rolled_back`, „Nichts wurde geändert …“). BEGIN, COMMIT, ROLLBACK,
  SAVEPOINT, RELEASE und `PRAGMA foreign_keys` mit Wert sind dort gesperrt. Getrennt werden die Befehle mit
  `sqlite3.complete_statement` (`_console_statements`: Semikolons in Texten, Kommentaren und Trigger-Rümpfen trennen nicht);
  Grenzen der Eingabe vor jeder Ausführung: 10 MB, 200000 Befehle, je Befehl 2000 geprüfte Semikolons und ein Budget für das
  erneute Lesen. Ausgabe bekommt jeder Befehl mit Spalten (auch WITH, PRAGMA, EXPLAIN, RETURNING). Die Verbindung hat keinen
  Statement-Cache (sonst fragt SQLite einen gleich lautenden Befehl nicht erneut den Authorizer). Nach dem COMMIT gehen die
  schreibenden Befehle ins SQL-Protokoll (`log_sql`, Beschreibung „SQL-Konsole (<Benutzer>)“, ohne Kommentare –
  `_console_log_text`); scheitert das, trägt die Antwort `warnings`. Es läuft **eine Anfrage zur Zeit** je Prozess
  (`_console_lock`, die zweite bekommt sofort eine Meldung), die Fehlerliste endet nach 100 Einträgen, und eine Anfrage bricht
  ab, wenn der freie Platz im Daten- oder Temp-Ordner seit ihrem Beginn um mehr als 1 GB gesunken ist oder gesunken ist und
  unter 256 MB liegt. Meldungen der Seite nur über `textContent` (`sqlStatus`). Die Vorlage „Zeitbereich löschen“ ist für den
  Schreibmodus gebaut (kein eigenes BEGIN/COMMIT) und hängt Sätze außerhalb des Zeitraums unter einer Bankbewegung des
  Zeitraums ab, wie `remove_split_row`. Der MCP-Server schickt nie `write`
- **Zeitraum-Parameter** (`from`/`to` in der Query, Cookie `period`) gelten nur als
  ISO-Datum (`period.parse_iso_date`/`valid_period`); `resolve_period` verwirft alles
  andere, Weiterleitungen aus Formularen (`handlers._period_args`) lassen einen
  ungültigen Zeitraum weg. In HTML maskieren (`html.escape`), in Adressen innerhalb von
  JavaScript prozentkodieren (`quote`) – ein maskiertes `'` löst der Browser im
  `onclick` wieder auf. `send_header` in `server/app.py` entfernt CR/LF/NUL aus jedem
  Header und ersetzt Zeichen außerhalb von Latin-1 durch `?`: letzte Sicherung gegen
  Header-Injection (und einen 500), egal woher ein Wert stammt
- **JSON in `<script>`-Blöcken** nur über `pages.script_json`, nie über `json.dumps`
  allein: Ein `</script>` im Wert beendet den Block (XSS). In HTML-Attributen
  (`onclick="…"`) das Ergebnis zusätzlich durch `html.escape`
- **Ausgabe:** Jeder Wert aus Datenbank oder Anfrage wird beim Einsetzen maskiert – in
  HTML-Text und -Attributen mit `html.escape(str(wert), quote=True)`, in einem
  `<script>`-Block nur über `pages.script_json`, in einer Adresse innerhalb von Inline-JS
  mit `urllib.parse.quote(…, safe='')`. Im Browser-JS nie Daten per `innerHTML` oder
  Template-String in HTML setzen, sondern `textContent`, `createElement`, `.value = …`.
  Richtext (Einleitungs-/Schlusstext der Angebote) nur über die Positivliste
  `richtext.sanitize_richtext` (b, strong, i, em, u, p, br, ul, ol, li, div, span, ohne
  Attribute, höchstens 50 Ebenen tief): beim Speichern UND bei jeder Ausgabe – Formular wie
  PDF-Satz –, auch für Altbestand. Jede Antwort trägt
  `X-Content-Type-Options: nosniff` und `X-Frame-Options: DENY` (`end_headers`); die App
  rahmt sich nicht selbst ein (PDFs öffnen als eigene Seite in einem neuen Tab). Jede Antwort trägt
  genau einen `Cache-Control` (seit 2.5.15) – den des Handlers (`serve_static_file`), sonst `no-store` für
  `/backup/download`, `/receipts/file` und die Suchen und `no-cache` für alles andere; entschieden wird an der
  Route, nie an der Query, eine Endungs-Regel gibt es nicht (`tests/test_cache_headers.py`)
- **Logos** (seit 2.3.3): Als Logo gelten nur Bilddateien der Positivliste `export/pdf_core.LOGO_CONTENT_TYPES` (PNG, JPG, GIF,
  WEBP – kein SVG). Sie gilt an allen vier Stellen: Upload (`handle_logo_upload`: Endung UND Kennbytes müssen passen, Anfrage
  höchstens 5 MB, geprüft an `Content-Length` vor dem Lesen), Auslieferung (`/data/logos/`, `/seed_data/private/`: alles andere 404),
  Einbettung ins PDF (`resolve_logo_path`) und Hinweis im Kontaktformular (`logo_on_server`). `resolve_logo_path` nimmt vom
  gespeicherten Wert nur den Dateinamen und sucht ihn im `logos`-Verzeichnis des Benutzers (mit `realpath` geprüft; daneben nur
  `seed_data/private`): Ein frei eingetragener Serverpfad ergibt kein Logo – nicht aufweichen

## Zugriffsschutz (HTTP), Fortsetzung

- **E-Mail-Versand** (`email_sender.py`, seit 2.3.3): immer verschlüsselt und mit Zertifikatsprüfung (`ssl.create_default_context()`;
  Port 465 `SMTP_SSL`, sonst STARTTLS – ohne STARTTLS-Angebot wird nicht angemeldet). Es gibt keinen Schalter zum Abschalten; ein
  eigenes CA-Zertifikat kommt über `SSL_CERT_FILE` (`DEPLOYMENT.md`). Meldungen an den Nutzer nennen nie Exception-Text oder
  Serverpfade (der volle Fehler steht im Server-Log); Namen gehen über `formataddr` in den Kopf, CR/LF wird abgelehnt
- **Anfragegrößen (seit 2.3.6, `server/app.py`):** Kein Body wird gelesen, bevor seine erklärte Länge geprüft ist
  (`_declared_length`: genau EIN `Content-Length` aus ASCII-Ziffern, kein `Transfer-Encoding`; sonst 400). Grenzen: 64 KB ohne
  angemeldeten Benutzer (`MAX_PREAUTH_BODY`: `/login`, `/logout`, Ersteinrichtung), 10 MB Formulare und JSON (`MAX_FORM_BODY`),
  200 MB Uploads (`MAX_UPLOAD_BODY`: `/upload_receipts`, `/receipts/upload`, `/wiso/import`), Logos 5 MB; darüber 413,
  ungelesen, Verbindung zu (`_refuse_body`). Neue Routen lesen ihren Body über `_read_body(limit)` bzw. prüfen vorher mit `_check_body_size` – nie
  `rfile.read(int(Content-Length))`. `_drain_body` liest höchstens 1 MB weg, sonst schließt es die Verbindung. Einzige größere
  Grenze: die WISO-Datenbank mit 2 GB je Datei (`wiso_upload.MAX_WISO_DB_BODY`, `/wiso/fdb-upload`) – sie wird nie im Ganzen
  gelesen, sondern in Stücken auf die Platte geschrieben, und nur, wenn danach noch 64 MB frei bleiben (sonst 507)
- **Zeitgrenze je Verbindung (seit 2.5.8, `server/app.py`):** Auf einer Verbindung, auf der `CONNECTION_TIMEOUT` = 120 s lang nichts mehr
  gelesen oder gesendet wird, gibt der Server auf (`SimpleWebServer.timeout`: je Lese- bzw. Sendeschritt, nicht für die ganze Anfrage).
  Ein Client, der seinen Body nicht zu Ende schickt, die Antwort nicht abholt oder eine Keep-Alive-Verbindung offen lässt, hält damit
  weder einen Thread noch die Lesesperre des Datenverzeichnisses (`_run_locked`) länger. Antworten gehen in Stücken von `SEND_CHUNK` =
  256 KiB hinaus (`_SteppedWriter`; sonst gälte die Grenze für die ganze Antwort); nach einem Fehlschlag wirft jeder weitere Schreibzugriff
  sofort, eine Fehlerantwort wartet nicht noch einmal die Frist ab. Ein Body, der nicht zu Ende kommt: 408 und Verbindung zu
  (`_read_body`); wo ein Handler den Body selbst liest, schließt `_TimedReader` die Verbindung in jedem Fall. Die Bearbeitung selbst hat
  keine Zeitgrenze (die SQL-Konsole hat ihre eigenen 60 s), und ein Upload, der stetig sendet, läuft beliebig lange. Nicht abschalten:
  Die Lesesperre des Datenverzeichnisses hinge sonst an einer hängengebliebenen Verbindung (`tests/test_connection_timeout.py`).
  **Jede Anfrage wird abgeschlossen** (`_settle_body`, nach der Antwort in `handle_one_request`): Was vom erklärten Body ungelesen
  blieb – eine GET-Anfrage mit `Content-Length`, eine frühe Ablehnung –, darf nie als nächste Anfrage gelesen werden. `_TimedReader`
  zählt die gelesenen Bytes (`count`, nach den Kopfzeilen 0); ein Rest bis `MAX_DRAIN_BODY` wird mit `DRAIN_TIMEOUT` = 2 s weggelesen
  (dann bleibt die Verbindung benutzbar), bei größerem Rest, `Transfer-Encoding`, ungültiger Länge oder ausbleibendem Rest geht sie zu.
  Ein neuer Zweig braucht dafür nichts zu tun – die Prüfung nicht umgehen (`tests/test_request_body_sync.py`). **Log:** Eine
  Verbindung im Leerlauf wird still geschlossen; ließ sich eine Antwort nicht zustellen (`wfile.failed`), reichen die breiten
  `except`-Zweige den Fehler weiter (`if self.wfile.failed: raise`), statt eine 500 zu versuchen – es bleibt eine Zeile im Log.
  **TLS (seit 2.5.9):** Bei eigenem TLS (`build_server`) läuft der Handshake je Verbindung in ihrem Thread
  (`SimpleWebServer.setup()`, Zeitgrenze `TLS_HANDSHAKE_TIMEOUT` = 15 s für den ganzen Handshake), nie in `accept()`
  (`do_handshake_on_connect=False`): Die Annahme neuer Verbindungen wartet nie auf einen Handshake – nicht zurückdrehen. Ein
  gescheiterter Handshake endet still (`handle()`: kein Log-Eintrag, keine Antwort; `tests/test_tls_handshake.py`). Die Warteschlange
  angenommener Verbindungen ist `ACCEPT_QUEUE` = 128 lang (mit und ohne TLS; der Standard der Bibliothek, 5, ließ bei vielen
  gleichzeitigen Verbindungen einzelne im Betriebssystem liegen)
- **Anmeldung (seit 2.3.6, `auth.attempt_login`):** Die Anmeldung im Browser geht nur darüber (`handlers.handle_login` →
  `LoginOutcome`), nie direkt über `authenticate`. Nach fünf Fehlversuchen in Folge ist der Name (ohne Groß-/Kleinschreibung)
  30 s gesperrt, je weiterem Fehlversuch doppelt, höchstens 900 s (429 mit `Retry-After`); während der Sperre wird nichts geprüft,
  gezählt oder verlängert, auch nicht mit dem richtigen Passwort. Gezählt wird VOR dem Hash (parallele Versuche zählen mit).
  Unbekannte Namen verhalten sich wie vorhandene (eigene, auf 10000 Einträge begrenzte Tabelle; Einträge vorhandener Benutzer
  werden nie verdrängt). Höchstens zwei scrypt-Läufe gleichzeitig (`_HASH_SLOTS`; nach 10 s Warten 503). Alles im Speicher: Ein
  Neustart hebt Sperren auf. Das Server-Log nennt bei einer Sperre die Adresse, nie den eingegebenen Namen (dort landet
  gelegentlich ein Passwort). Mindestlänge beim Setzen eines Passworts: 8. `set_password` beendet die Sitzungen des Benutzers
  (außer `keep_session`: wer sein eigenes ändert), das Ein- und Ausschalten der automatischen Anmeldung alle
  (`delete_all_sessions`). Tests: `conftest` leert die Drosselung vor jedem Test (`reset_login_throttle`)
- **`Secure`-Cookie:** bei eigenem TLS oder wenn ein Proxy `X-Forwarded-Proto: https` meldet (`forwarded_https`, erster Wert).
  Der Kopf steuert nur das Cookie-Flag, sonst nichts
- **IDs aus der Adresse** nur über `_int_param` (Filter: fehlt/ungültig = nicht angegeben), `_id_param` (Seite zu einem Objekt:
  fehlt/ungültig = −1, also wie eine unbekannte ID) und `self._query()`; nie `int(qs[…][0])` oder `split('?')[1]`. Was nicht in
  64 Bit passt, gilt als ungültig. Aktionen ohne gültige ID: 400 (`tests/test_bad_addresses.py` prüft `app.py` statisch)
- **Formularwerte (seit 2.4.2, `server/handlers.py`):** IDs aus Formularen nur über `_form_id` (positive ASCII-Ganzzahl in 64 Bit; optionale
  Felder über `_optional_form_ids`), Ganzzahlen nur über `_form_int(text, minimum, maximum, default)` (ASCII-Ziffern im Bereich: Jahr 1900–2999,
  Prozent 0–100, Nutzungsdauer 1–100, Zählerstand 0–999 999 999), Kommazahlen nur über `_finite_number` – nie `int(post_data…)`/`float(post_data…)`.
  **Buchungs- und Steuerdatum der Buchungsmaske (seit 2.9.1):** nur ein Kalendertag `JJJJ-MM-TT` (`parse_iso_date`; das Steuerdatum darf leer sein), sonst 400
  (`BOOKING_DATE_INVALID`/`TAX_DATE_INVALID`) vor dem ersten Schreiben, beim Anlegen wie beim Ändern – der Browser sorgt über das Datumsfeld dafür, bis 2.9.0
  speicherte der Server jeden Text, und die Zeile fehlte in jeder Auswertung (`tests/test_booking_date_strict.py`).
  Ungültig: die Meldung auf dem Fehlerweg der Seite (303 mit `err=`/`error=`/`msg=`, Klartext über `quote(…, safe='')`, angezeigt nur über
  `script_json` + `appMsg`); bei einer ungültigen ID und auf Seiten ohne Fehlerweg 400 mit Klartext. Nichts wird gespeichert. Ein Handler gibt
  `(Status, Text)` zurück, `server/app.py` antwortet über `_respond_form_result`: ab 400 steht der Text im Body (`text/plain`), nie im
  `Location`-Kopf (sonst bliebe die Seite leer). **Nie Exception-Text in einer Antwort:** ein fester Satz, der volle Fehler nur ins Server-Log
  (`traceback.print_exc()`); Klartext kommt nur aus eigenen Ablehnungsklassen (`_Refused`, `DeleteRefused`, `ContactRefused`,
  `BackupRefused` …). Eine doppelte Kunden-/Kontaktnummer oder ein doppeltes Kürzel wirft `ContactRefused` (Rollback, Meldung
  auf der Kontaktseite). `tests/test_form_error_paths.py` prüft es und hält statisch fest, dass `handlers.py` weder
  `int(post_data…)`/`float(post_data…)` noch einen 5xx mit Ausnahmetext noch eine Weiterleitung (3xx) mit dem Text einer
  Ausnahme aus einem breiten `except` enthält. **Die Meldungen auf *Daten* (seit 2.5.5, `msg=`):** Sicherung,
  Wiederherstellung, DATEV-Export und WISO-Import (Textexport wie Datenbank) geben nur den Klartext einer `BackupRefused`
  weiter, sonst einen festen Satz (`BACKUP_FAILED`, `RESTORE_FAILED`, `DATEV_FAILED`, `WISO_IMPORT_FAILED`); der volle Fehler
  steht im Server-Log. Die Seite liest `msg` nicht im Browser, sondern bekommt es vom Server als JSON im Skriptblock `miscMsg`
  (`script_json`, höchstens `MSG_MAX_LENGTH` = 2000 Zeichen – die längste Meldung des DATEV-Exports hat gut 1500) und zeigt es
  nur über `textContent`/`appMsg` (`tests/test_misc_messages.py`). Der Beleg-Upload (`server/upload_handler.py`) nennt je Datei nur
  den Klartext eigener Klassen (`CamtError`, `DkbCsvError`, `document_parser.DocumentExists`), sonst `UPLOAD_FAILED`; die Belege-Seite
  zeigt das Ergebnis je Datei als Text und lädt nur ohne Warnung neu (`tests/test_upload_feedback.py`)
- **Fehlerantworten über `send_error`** (`server/app.py`): tragen immer Statuszeile und Schutz-Header, auch bei einer kaputten
  Anfragezeile – unabhängig vom Patch-Stand der Standardbibliothek (eine echte HTTP/0.9-Anfrage bekommt ihre normale Antwort
  weiter ohne Kopf)
