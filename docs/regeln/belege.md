# Belege: Dateien, Verknüpfen, Belegerkennung

## Belegdateien und Verknüpfen

- **Belegdateien (seit 2.5.11, `server/receipt_files.py`):** Jede Datei, die die Seite *Belege* hochlädt, wird sofort ein Beleg (Nummer aus
  dem Kreis „Belegnummern Firma“, Datum, Dateiname) und liegt unter `<Datenordner>/Belege/<Jahr des Belegdatums>/<Nummer>_<Name>`
  (`Database.insert_receipt_with_file`; `Documents.Path` relativ zum Datenordner, mit `/`). Den Pfad setzt der Server, nie der Client.
  Ein Kontoauszug ist dort ein Beleg wie jede andere Datei (`receipt_date` liest ihn nur für das Datum und den Hinweis auf den Import der
  Seite *Buchungen*): kein Import, nichts in `pending_imports/`; `POST /upload_receipts` bleibt der Import dieser Seite.
  **Upload** `POST /receipts/upload` (`_UPLOAD_PATHS`, JSON, GET → 405): vor dem ersten gelesenen Byte CSRF-Token (Header), Demo-Modus
  (403 mit `RECEIPTS_DEMO_REFUSED`, Verbindung zu; die Seite zeigt den Satz statt der Ablage) und Größe (`_read_body(MAX_UPLOAD_BODY)`),
  höchstens 100 Dateien je Upload. **Je Datei gehören Nummer, Datei und Eintrag zusammen:** Der Inhalt wird VOR der Transaktion als Teildatei
  (`<32 Hex-Zeichen>.part`, `O_CREAT|O_EXCL|O_NOFOLLOW`) in den Jahresordner geschrieben – das Schreiben von bis zu 200 MB hält keine
  Schreibsperre; der Jahresordner darf kein Link sein (`year_folder`, sonst scheitert die Datei und nichts ist angelegt; `Belege/` selbst darf einer
  sein). In EINER Transaktion (`BEGIN IMMEDIATE`) folgen Nummer, Name (`reserve_name` legt den Platz als leere Datei mit
  `O_CREAT|O_EXCL|O_NOFOLLOW` an: nie wird eine vorhandene überschrieben, ein belegter Name bekommt `_2`, `_3` …), `os.replace` der Teildatei auf
  diesen Platz und der Eintrag. Scheitert ein Schritt, auch das COMMIT, ist der Zähler nicht verbraucht (und kein Kreis angelegt), es gibt keinen
  Eintrag, und weder Datei noch Teildatei bleibt liegen (erst werden die Dateien entfernt, dann wird zurückgerollt; ein scheiterndes Zurückrollen
  verdeckt die Ausnahme nicht); die Antwort nennt je Datei nur feste Sätze (`RECEIPT_UPLOAD_FAILED` …), nie Ausnahmetext oder Pfad. Der Name wird
  bereinigt (`safe_receipt_name`: nur der Name, `[A-Za-z0-9ÄÖÜäöüß ._()-]`, höchstens 120 Zeichen, kein Windows-Gerätename); der abgelegte Name
  (`stored_file_name`: `<Nummer>_<Name>`) hat höchstens 200 Bytes in UTF-8 (die Nummer höchstens 60; gekürzt wird der Stamm, die Endung bleibt)
  und heißt nie `Konten` oder `general` – so heißen die Ordner des Kontoauszug-Imports im Jahresordner (ein Unterstrich davor). Der angezeigte Name
  (`display_name`) trägt weder Steuerzeichen noch unsichtbare Formatzeichen (Unicode-Kategorien `Cc` und `Cf`, auch U+202E).
  **Die Nummer** (`take_receipt_number`): der Kreis des Jahres des Belegdatums, sonst der jüngste – wie `peek_next_number`; gibt es noch gar keinen
  Kreis „Belegnummern Firma“, entsteht einer für das **laufende** Jahr (Buchstabe `B`, Standardvorlage, in der Transaktion des Belegs) – sonst nie von
  selbst, auch nicht zum Jahreswechsel, und eine eigene Nummer ohne Kreis bildet keinen; liefert die Vorlage eines Kreises keinen Zähler, hat der
  Beleg keine Nummer, die Antwort einen Hinweis. Keine Prüfung auf belegte Nummern (nicht eindeutig, derselbe Kreis nummeriert die Beleg-Nr. der
  Buchungen). Eine eigene Nummer hebt den Zähler eines Kreises nur, wenn sie zu dessen Vorlage passt und ihre laufende Nummer in 1 bis
  `MAX_RECEIPT_RUNNING` (999 999 999) liegt; eine größere gilt als Text, ohne Fehler. `consume_number` (die Buchungsmaske) hebt den Zähler auf die
  gespeicherte Nummer, wenn sie über ihm liegt – nie nach unten: Hat das Löschen eines Belegs den Zähler zurückgenommen, wird eine Nummer, die
  eine Buchung trägt, nicht noch einmal vergeben. Das Formular „Beleg hinzufügen“ (ohne Datei) verbraucht die Nummer ebenso (`number_suggestion`:
  eine unveränderte Nummer wird frisch vergeben) und nimmt nie einen Pfad. Beim Löschen des Belegs mit der zuletzt vergebenen Nummer geht der
  Zähler um eins zurück, wenn die Nummer genau die ist, die der Kreis für ihre laufende Nummer vergibt („26B0001“ ist nicht „26B001“), und kein
  anderer Beleg und keine Buchung sie trägt (ohne Rücksicht auf Groß-/Kleinschreibung; `release_receipt_number`, in der Transaktion des Löschens);
  eine Nummer mitten im Kreis bleibt eine Lücke und wird nie neu vergeben, umnummeriert wird nie. **Ansehen und Herunterladen**
  (`GET /receipts/file?id=<n>[&inline=1]`, rein lesend, ID über `_id_param`, Herkunft wie
  bei `/backup/download`): Ein gespeicherter Pfad wird nur über `receipt_files.stored_receipt_path` geöffnet oder gelöscht und gilt nur
  innerhalb von `Belege/` und `Documents/` (Bestand des früheren Uploads) des angemeldeten Benutzers – mit `realpath` aufgelöst, mit
  `commonpath` geprüft, höchstens `STORED_PATH_LIMIT` = 1024 Zeichen lang; alles andere ist 404 mit festem Satz, der Pfad steht nur im Server-Log. Geöffnet wird mit `O_NOFOLLOW`/`O_NONBLOCK`
  und `fstat` (nur reguläre Dateien), gesendet in Stücken (`backup.copy_to`, Zeitgrenze je Stück) mit `no-store`. Angezeigt wird nur mit
  `inline=1` UND wenn Endung und Kennbytes zur Positivliste passen (`inline_type`: PDF und die Rasterbilder der Logos); alles andere – auch
  SVG, HTML, XML – ist `attachment` mit `application/octet-stream`. Die Route steht nicht in der Positivliste des Diagnose-MCP.
  **Löschen** (`POST /receipts/delete`): Die Datei geht mit – nur, was der Upload selbst ablegt: der gespeicherte Wert `Belege/<JJJJ>/<Name>`
  (relativ, vier Ziffern, weder Jahresordner noch Datei ein Link; `receipt_files._uploaded_shape` – dieselbe Stelle entscheidet, ob die Rückfrage
  „und seine Datei“ lautet). Nicht der Bestand in `Documents/`, nicht, was der Kontoauszug-Import unter `Belege/<Jahr>/Konten/` oder
  `Belege/<Jahr>/general/` ablegt, kein absoluter Pfad, kein `..`; dazu nur eine reguläre Datei, nur wenn kein anderer Beleg auf dieselbe
  aufgelöste Datei zeigt, nie nach einer Ablehnung (`remove_receipt_file`). Das Formular ändert den gespeicherten Pfad nie.
  `tests/test_receipt_upload.py`, `tests/test_receipt_file.py`, `tests/test_delete_refusals.py`, `tests/test_suggestion.py`
- **Belege verknüpfen (seit 2.5.12, `server/receipt_links.py`, `Database.link_receipt_to_booking`):** An der Buchung (Maske, nach `</form>`, eigene
  `fetch`-Aufrufe, kein Formularfeld) wird ein Beleg gesucht und zugeordnet, am Beleg (`/receipts/edit?id=<n>`, ID über `_id_param`) eine Buchung.
  **Suchen:** `GET /receipts/search` und `GET /transactions/search` (`_SEARCH_PATHS`; `q` höchstens 100 Zeichen, sonst 400; `limit` 1–50, sonst 20, auch in
  der Datenbankschicht auf 1–50 begrenzt; Herkunft geprüft; rein lesend; ohne Rücksicht auf die Schreibweise über `ub_fold` + `INSTR`, kein `LIKE`; ein
  Datum oder dessen Anfang und ein Betrag finden ebenfalls; die Antwort nennt nie einen Pfad, nur `none`/`outside`/`ok`; `Cache-Control: no-store`, auch bei
  einem Suchbegriff wie `.png`; ihre 503 während einer Wiederherstellung kommt als JSON; nicht in der Positivliste des Diagnose-MCP; seit 2.6.0 steht eine dritte Route
  unter denselben Regeln in `_SEARCH_PATHS`: `GET /receipts/suggestions`, siehe *Belegerkennung*). **Schreiben:**
  `POST /bookings/link-document` und `/bookings/unlink-document` (`_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`, JSON `{bookingId, documentId[, relationType]}`,
  IDs streng über `_json_positive_id`; `relationType` aus `RELATION_TYPES`, sonst `receipt`; unbekannte Buchung oder Beleg 404 mit Klartext). Verknüpfen ist
  idempotent und läuft in EINER Transaktion: Prüfen, Zeile, Beleg-Nr. **Die übernommene Nummer wirkt wie eine von Hand eingetragene, auch auf den
  automatischen Abgleich** (dieselbe Beleg-Nr. am selben Tag fasst eigenständige Sätze zu EINEM Beleg zusammen, Stufe 4 vergleicht die Nummer einer
  Bankbewegung als Teilzeichenkette) – deshalb übernimmt die Buchung die Nummer des Belegs (`update_booking_fields(tx=…)`, Replay-Log erst nach dem Commit)
  nur, wenn **alle** Bedingungen zutreffen, in dieser Reihenfolge: (1) sie trägt keine Beleg-Nr. (leer oder nur Leerraum); (2) der Beleg trägt eine Nummer;
  (3) die Verknüpfung ist von der Art `receipt` – maßgeblich ist die Art, die nach dem Aufruf in `BookingDocuments` steht (eine bestehende Verknüpfung
  behält ihre, auch bei erneutem Zuordnen; `invoice`, `contract` und `other` übernehmen nie); (4) kein Zahlungssatz (`invoice_payment`); (5) keine Rechnung
  trägt die Nummer (`Invoices.InvoiceNumber`); (6) keine **andere** Buchung trägt diese oder eine ähnliche Nummer – ähnlich: die eine ist in der anderen
  enthalten, in beiden Richtungen, ohne Leerraum am Rand und ohne Rücksicht auf die Schreibweise (`26B001` gegen `26B0010` und `26B001_A`;
  `_NUMBER_IN_USE_SQL`, ein Wert aus einem Altbestand, der kein Text ist – Zahl, Blob –, gilt als sein Text). Die eigene Familie zählt dabei nicht (sie gehört
  zum selben Vorgang, der Abgleich fasst sie nicht an): die Sätze der Buchung, ihre Bankbewegung und deren übrige Sätze; eine fremde Bankbewegung und deren
  Sätze schon. **Diese Bedingungen nicht lockern** – lieber keine Nummer als eine, die der Abgleich als Beleg liest. Die Prüfung (6) liest die Tabelle einmal
  (kein Index auf `DocumentNumber`; gemessen rund 50 ms bei 200 000 Buchungen, unter der Schreibsperre). Nur die verknüpfte Zeile (Kinder einer Bankbewegung
  erben nichts, wie beim Speichern der Maske). Unterbleibt die Übernahme allein an (6), sagt es die Antwort (`numberInUse`) und die Seite („Die Beleg-Nr. wurde
  nicht übernommen: …“, als Hinweis, nicht als Erfolg); `hasEntries` ist wahr bei einer Bankbewegung mit Buchungssätzen, die Meldung sagt dann, dass diese ihre
  eigenen Nummern behalten. Lösen entfernt nur die Verknüpfung, die Nummer bleibt (sie bleibt auch beim Löschen des Belegs in Gebrauch). **Die Listen der Antwort**
  (`documents`, `bookings`) liest `handlers._link_lists` NACH dem Commit: Scheitert das, bleibt die Antwort 200 mit leeren Listen und `stale: true` – nie 500, die
  Verknüpfung ist gespeichert –, die Seite zeichnet dann nichts neu und bittet um Neuladen. **Eine Antwort gehört zur geöffneten Buchung:** `#txReceipts` trägt
  `data-booking`; `txAttach`/`txDetach` halten die ID beim Aufruf fest und prüfen im Antwortzweig als Erstes `txStill(<ID>)` (die Maske wird beim Öffnen einer anderen
  oder neuen Buchung ausgetauscht) – sonst fassen sie weder Liste noch Feld Beleg-Nr. noch Meldung an; die Treffer von `txReceiptSearch` werden ebenso nur dann
  gezeichnet. Die Seiten bekommen die Daten als JSON im Skriptblock (`script_json`);
  das Skript (`LINK_WIDGET_JS`, `RECEIPT_LINK_JS`, `TX_LINK_JS`) setzt sie nur über `textContent`/`createElement`/`.value`, nur `var` und `function`.
  **Bekannte Grenzen der übernommenen Nummer:** Bekommt eine aus dem WISO-Textexport importierte Buchung ohne Beleg-Nr. so eine Nummer, erkennt ein erneuter
  Import derselben Textdatei sie nicht wieder und legt sie ohne Meldung ein zweites Mal an; die Übernahme CSV → WISO-Datenbank nimmt sie nicht (neu angelegt,
  als mögliches Duplikat gemeldet); ein Satz mit bekannter Quellgruppe wird beim erneuten Import der Datenbank weiter wiedererkannt; legt WISO ihn unter neuer
  Quellgruppe neu an, findet die Neuzuordnung ihn nicht (neu angelegt, die alte Zeile gemeldet). Eine vor dem Zuordnen geöffnete Maske (zweites Fenster), die
  danach gespeichert wird, schreibt ihr leeres Feld Beleg-Nr. zurück; eine schon exportierte Buchung trägt nach dem Zuordnen ein Belegfeld 1.
  `/documents/link` und `/documents/unlink` gibt es nicht mehr. `tests/test_receipt_links.py`

## Belegerkennung

- **Grundsatz:** Ein gelesener Wert ist eine Lesung der Datei, nicht der Inhalt des Belegs. Die Lesung steht in `DocumentReadings` (eine Zeile je
  Beleg, `db/document_readings.py`), getrennt von dem, was der Nutzer am Beleg pflegt (`Documents`: Nummer, Datum, Dateiname, Info). **Nichts wird
  ohne Klick verknüpft, gebucht oder am Beleg geändert – nicht einführen.** Umfang heute: Felder lesen und mit ihrer Herkunft zeigen, eine
  Bankbewegung vorschlagen. Kein OCR (ein Scan oder Bild ist `no_text`), kein Sprachmodell. Beträge stehen in Minor Units (`money.SCALE` = 4, also
  nicht Cent), der Steuersatz als Anteil (0,19)
- **Das Paket `receipt_reading/` ist rein:** Es bekommt Bytes, schreibt nichts, öffnet keine Dateien und kennt weder `server/` noch `db/`;
  `read_document` wirft nie (jede Ausnahme ist eine Lesung `failed` mit Befund `read_error`, ohne Ausnahmetext). **Erkannt wird nach Endung UND
  Kennbytes** (`detect.kind`: `.pdf` mit `%PDF-`, `.xml` mit `<` nach BOM und Leerraum, Bild = Positivliste der Logos), nie nach nur einem von beiden.
  **Reihenfolge:** zuerst die E-Rechnung (`einvoice.py`: hochgeladenes XML – UBL `Invoice`/`CreditNote`, CII –, bei einer PDF das eingebettete
  `factur-x.xml`/`zugferd-invoice.xml`/`xrechnung.xml`, `pdf_embedded.py`); liefert sie kein lesbares Feld (`einvoice_empty`), kommen die Textregeln
  (`pdf_text.py`, `text_rules.py`). Gelesen werden BT-1 Nummer, BT-2 Datum, BT-5 Währung, BT-27 Verkäufer (UBL ersatzweise BT-28), BT-31 USt-IdNr.,
  BT-109 Netto, BT-110 Steuer, BT-112 Brutto, der Satz aus BG-23 (nur, wenn die Aufschlüsselung genau einen nennt) und die Art (BT-3 bzw. Wurzel:
  `invoice`/`credit_note`; eine Gutschrift trägt positive Beträge); BT-115 steht nur in `raw`
- **Strenge Werte, Befunde, Version, Grenzen:** Nur ASCII-Ziffern, ein Betrag mit höchstens zwei Nachkommastellen und zwölf Stellen davor, ein
  Kalenderdatum der Jahre 1900–2200, die Währung als drei Großbuchstaben, ein Betrag, dessen `currencyID` von BT-5 abweicht, gilt nicht – **was nicht
  streng passt, bleibt leer und bekommt den Befund `bad_<Feld>`, nie geraten**. Abgeleitet wird genau ein Wert: die Steuer 0, wenn der Gesamtbetrag
  der Steuer fehlt und die Aufschlüsselung nur Nullen nennt (Beleg ohne Steuerausweis). **Status** (`STATUSES`): `ok`, `empty` (lesbares Dokument ohne
  lesbares Feld), `no_text`, `unsupported`, `too_large`, `failed`; **Quelle** (`SOURCES`): `einvoice_cii`, `einvoice_ubl`, `text`. Die Befund-Codes
  stehen als Tabelle im Kopf von `receipt_reading/__init__.py`. **`READER_VERSION` steigt um 1, sobald sich die Leseregeln ändern:** Ältere Lesungen
  gelten dann als ungelesen (`unread_receipt_ids`) und zeigen „(ältere Regeln)“; neu gelesen wird nie still. **Grenzen** (Konstanten, bei jedem Aufruf
  gelesen – die Tests verkleinern sie): `MAX_READ_BYTES` 30 MB (darüber `too_large`, ohne Prozess), `MAX_XML_BYTES` 20 MB (auch entpackt),
  `MAX_EXTRACT_CHARS` 200 000 Zeichen aus einer PDF – gelesen werden nur die ersten 5 und die letzten 3 Seiten (`pdf_text.HEAD_PAGES`/`TAIL_PAGES`;
  unter 20 sichtbaren Zeichen: `no_text`) –, gespeichert höchstens `MAX_TEXT_CHARS` 20 000 Zeichen Text; `Reading.to_row` kappt Absender (200
  Zeichen), USt-IdNr. (20), Nummer (100) und `Raw` samt Befunden (`MAX_RAW_BYTES` 64 KB: erst fallen Zitate und Aufschlüsselung weg, Befund
  `raw_truncated`)
- **Schutz (die Datei kommt aus einem Upload) – nicht aufweichen:** XML nur über `importers.safe_xml.parse_untrusted_xml` (Größe vorab, jede DTD und
  Entity-Deklaration abgelehnt, `UnsafeXml`; `importers/camt.py` nutzt dieselbe Stelle). Der Anhang einer PDF wird nur begrenzt entpackt
  (`pdf_embedded._bounded_inflate`: nur ein Flate-Filter ohne Predictor, höchstens `MAX_XML_BYTES` + 1 Bytes, nie `PDFStream.get_data()`;
  höchstens `MAX_ATTEMPTS` = 3 Anhänge mit dem Namen einer E-Rechnung werden entpackt – dieselbe Datei über `AF` und den Namensbaum, gleiches
  Stromobjekt, zählt einmal –, danach endet die Suche mit dem bisherigen Befund `attachment_unreadable`). **Gelesen
  wird im Kindprozess:** `receipt_reading.isolated.read_isolated` startet `python -m receipt_reading.worker` (derselbe Interpreter, Datei und Angaben
  der eigenen Firma über stdin) mit hartem Zeitlimit `READ_TIMEOUT` (15 s; Überschreitung: `too_large` mit Befund `time`), unter POSIX mit `RLIMIT_AS`
  und `RLIMIT_CPU`; die Ausgabe (höchstens 1 MB) wird streng über `Reading.from_dict` gegengeprüft, alles andere ist `failed` mit
  `reader_output`/`reader_crashed`. `read_isolated` wirft nie und nennt nie den Text des Kindprozesses (stderr steht höchstens gekürzt im Server-Log);
  der Server ruft `read_document` nie selbst auf
- **Textregeln (`text_rules.py`) – lieber leer als geraten:** Gearbeitet wird auf Zeilen (höchstens `MAX_LINES` zu je `MAX_LINE_CHARS` Zeichen,
  Leerraum zusammengefasst), jeder reguläre Ausdruck läuft in linearer Zeit. Ein Betrag ist nur `1.234,56`/`119,00` (Komma und genau zwei Stellen,
  Tausenderpunkte in Dreiergruppen) und kommt allein aus `money.parse_amount`; `19,00 %`, ein Betrag mit nicht eindeutigem **Vorzeichen** und der
  Rest einer Gruppierung (`1 234,56`) sind keiner. Nicht eindeutig ist das Vorzeichen, wenn davor ein Minus, Gedankenstrich oder eine öffnende Klammer
  steht (`- € 119,00`, `-EUR 119,00`, `(119,00)`) oder dahinter ein Minus oder ein Soll-/Haben-Zeichen (`119,00 -`, `119,00 €-`, `119,00 CR`/`DR`/`S`/`H`),
  dazwischen nur Leerraum und ein Währungszeichen (`_SIGN_BEFORE`/`_SIGN_AFTER`, EINE Stelle in `_scan_amounts`); `-119,00` unmittelbar davor ist ein
  negativer Betrag, ein Minus vor einem Buchstaben (`EUR-Konto`) oder mit Leerraum davor und einer Ziffer dahinter (`119,00 -5,00`) kein Vorzeichen
  dieses Betrags. **Steuer:** Zeilen mit Steuerwort (nicht die der USt-IdNr./Steuernummer, nicht die, in denen eine Brutto- oder Nettobezeichnung den
  Betrag beansprucht); der Betrag steht HINTER dem Steuerwort. Steht dort genau einer, ist er die Steuer – außer „auf“/„von“/„aus“ steht als Wort
  dazwischen (`19 % MwSt auf 100,00`: die Basis; die Zeile liefert keine Steuer). Stehen dort mehrere und nennt die Zeile genau einen Satz
  (`USt 19% 100,00 19,00 119,00`), ist es der Betrag, der als EINZIGER (gleiche Werte zählen einmal) die Satzprobe gegen einen ANDEREN Betrag der Zeile
  besteht (Basis × Satz auf den Cent – dieselbe Funktion wie die Satzprobe `_matches_net` – oder im Brutto enthalten); kein oder mehr als ein solcher
  Betrag, kein oder mehr als ein Satz oder mehr als `MAX_LINE_AMOUNTS` = 8 Beträge in der Zeile (eine Tabellenzeile; die Probe ist paarweise): keine
  Steuer, auch kein Befund. Bezeichnungen zählen als ganze Wörter und haben **Ränge** (Brutto: Rechnungsbetrag, Gesamtbetrag … vor
  Zahlbetrag vor „brutto“/„total“; Netto und Datum ebenso, eine Zeile mit Leistungs-, Liefer-, Fälligkeits- oder Bestellbezeichnung liefert kein
  Belegdatum): Nur der beste Rang, der überhaupt einen Treffer hat, zählt, der Betrag ist der erste HINTER der Bezeichnung derselben Zeile (steht die
  Gegenbezeichnung dazwischen, zählt das Paar nicht). **Mehrere verschiedene Werte für ein Feld: leer mit Befund `conflict_<Feld>`** – bei Brutto,
  Netto und Steuer nur dann nicht, wenn GENAU EINE Kombination die Summenprobe besteht (`by_checksum`). Der Absender ist nur ein Abschnitt der ersten
  Zeilen der ersten Seite, der wie ein Firmenname aussieht (Rechtsform als ganzes Wort); bei mehreren entscheidet der Fuß der letzten Seite, sonst
  `conflict_sender_name`. Die Proben stehen in `raw['checks']` (`sum`: Netto + Steuer = Brutto; `rate`: Netto × Satz = Steuer auf den Cent, nur bei
  genau einem Satz; je `ok`/`mismatch`/`incomplete`), **jedes gelesene Feld nennt Zeile und Seite**
  (`raw['fields'][<Feld>] = {value, source: 'rule', quote, page}`). **Ein Kontoauszug ist kein Beleg:** Erkennt ihn ein Importmodul
  (`importers.find_importer`) UND steht eine Saldozeile (`find_balances`) da, ist die Lesung `unsupported` mit Befund `statement` (der Text bleibt
  gespeichert). **Die eigene Firma** (`Database.own_identity`: Namen und USt-IdNrn. aller Kontakte mit `ContactType = 'own'`) ist nie der Absender und
  nie die USt-IdNr. des Belegs. **Die Währung gilt am Betrag** (`_currency_at`): Maßgeblich ist, was unmittelbar am gelesenen Betrag steht (davor oder
  dahinter, nur Leerraum dazwischen) – `€`/`EUR` ist Euro, `$`, `£`, `USD`, `CHF`, `GBP` machen den Betrag fremd –, am übernommenen Brutto, ohne Brutto
  an Netto und Steuer: Ein fremdes Zeichen dort lässt Beträge, Satz und Aufschlüsselung leer (`foreign_currency`), ein Euro-Zeichen setzt `EUR`
  (Zitat: die Zeile dieses Betrags). Steht am Betrag nichts, gilt der ganze Text wie bisher (Euro, wenn er Euro nennt) – aber ein fremdes Kürzel an
  einer Zahl und Euro nirgends an einer Zahl (nur „EUR-Konto“, „Preise in EUR“) macht die Beträge ebenfalls fremd. Lockern nur mit Tests für das,
  was NICHT gelesen wird
- **Gelesen wird nur auf Anfrage der Seite, nie im Upload selbst** (`server/receipt_reader.py`): `POST /receipts/read` (JSON `{documentId}`, ID streng
  über `_json_positive_id`; `_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`: GET 405, CSRF-Token und Herkunft wie jede JSON-Route) liest die Datei EINES
  Belegs; im Demo-Modus 403 (kein Knopf, nichts geschieht). **Es läuft höchstens ein Lesen je Prozess** (`_read_lock`), ein zweites bekommt sofort 409
  (`busy: true`). Ablauf von `read_receipt`: Beleg lesen (unbekannt: 404) → die Datei NUR über `receipt_files.open_receipt` (`Belege/`/`Documents/`,
  `O_NOFOLLOW`, reguläre Datei; ohne eine solche: Lesung `failed` mit Befund `no_file`) → Größe prüfen, Bytes in Stücken lesen, Datei schließen →
  `read_isolated` – **alles außerhalb einer Transaktion der Datenbank** – → `Database.save_document_reading(document_id, expected_path, row)`: Prüfung
  und Schreiben in EINER `BEGIN IMMEDIATE`-Transaktion, geschrieben wird nur, wenn der Beleg noch da ist und denselben Pfad trägt wie beim Lesen
  (sonst 409, nichts gespeichert); die vorhandene Lesung wird ersetzt. **Die Antwort nennt nie einen Pfad, den Namen der Datei auf der Platte, ihren
  ganzen Text (`Text`) oder einen Ausnahmetext:** feste Sätze, der volle Fehler nur im Server-Log (500); bei Erfolg 200 mit `reading`
  (`reading_payload`; dazu gehören die Zitate der gelesenen Zeilen, je höchstens 200 Zeichen) – auch für `failed`/`no_text`: Der Status sagt, was geschah. Am Beleg selbst ändert sich nichts. Die Seite *Belege* liest nach einem Upload die angelegten
  Belege nacheinander (`readReceipts`: je Beleg EINE Anfrage, bei `busy` nach 2 s noch einmal), dazu ein Kasten über der Liste mit einem Knopf für
  ungelesene (`count_unread_receipts`, höchstens `READ_BATCH` = 100 je Klick) und auf der Seite eines Belegs „Lesen“/„Neu lesen“. **Die Lesung ist
  abgeleitet:** ihre Schreibwege gehen nicht ins SQL-Protokoll (`_log_sql`), und sie geht beim Löschen des Belegs in dessen Transaktion mit
  (`delete_receipt`, ebenso die Vorlage „Zeitbereich löschen“ der SQL-Konsole) – sie zählt nie als Verweis im Sinn von `DeleteRefused`
- **Anzeige:** Spalte „Gelesen“ der Belegliste aus EINER Abfrage (`document_reading_summaries`, liest nie `Text` oder `Raw`; `summary_text`: Betrag ·
  Absender, sonst das Wort zum Status, mit „(ältere Regeln)“, `–` ohne Datei, `ungelesen` ohne Lesung; Kopf- und Datenzelle tragen die Klasse
  `readCell`, eine Mindestbreite in `buch.css`, damit der Text in schmalen Fenstern nicht auf viele Zeilen umbricht). Der Kasten „Gelesen“ auf der Seite eines
  Belegs (`pages_receipts._reading_box`, serverseitig, jeder Wert maskiert) nennt Status, Herkunft und Zeitpunkt (gespeichert UTC, gezeigt Ortszeit),
  die Felder mit ihrer Herkunft (bei den Textregeln das Zitat der Zeile, bei der E-Rechnung der BT-Bezeichner), die Proben (eine, die nicht stimmt, in
  Warnfarbe) und die Befunde – **nur als feste Sätze aus `receipt_reader.NOTE_TEXTS`:** ein unbekannter Schlüssel erscheint nie, und ein neuer
  Befund-Code im Kopf von `receipt_reading` braucht dort einen Satz (`test_every_note_code_of_the_reading_package_has_a_sentence`). `Raw` wird streng
  gelesen (kaputtes JSON ist leer), dazu steht immer der feste Hinweis `READING_HINT` („bitte prüfen“). **„Gelesenes Datum übernehmen“ setzt nur den
  Wert des Datumsfelds** (`receiptTakeDate`): gespeichert wird erst mit „Beleg aktualisieren“
- **Vorschläge Beleg → Buchung** (`db/receipt_matching.py` rein, `Database.suggest_bookings_for_receipt`, rein lesend): Es wird nur vorgeschlagen, nie
  verknüpft. Voraussetzung ist eine Lesung mit Status `ok`, Währung leer oder `EUR` und einem Brutto ungleich 0 (`usable_gross`) sowie ein Datum – das
  gelesene (`DocDate`), sonst das Belegdatum. **Nur, solange der Beleg an keiner Buchung hängt:** Hat er eine Zeile in `BookingDocuments` (gleich welcher
  Art der Verknüpfung, auch an einem Kind), liefert `suggest_bookings_for_receipt` `[]` – geprüft auf der Verbindung, die den Beleg liest, vor den
  Kandidaten (`Database.receipt_is_linked` stellt dieselbe Frage allein); nach dem Lösen der letzten Buchung kommen die Vorschläge wieder. **Kandidaten** (`receipt_match_candidates`: EINE Abfrage, alle Werte als Parameter, höchstens
  `MAX_CANDIDATES` = 200, älteste zuerst): Bankbewegungen und eigenständige Sätze, deren Betrag ohne Vorzeichen genau das Brutto ist (−119,00 passt zu
  119,00), mit Buchungsdatum von 7 Tagen vor bis 90 Tage nach dem Datum (`candidate_window`). **Ausgeschlossen:** das Kind einer Bankbewegung (sein
  Betrag ist ein Teil), der Zahlungssatz einer Rechnungszuordnung (`PAYMENT_MARK`), ein Spiegel (`AutoMirror` oder eine der `MIRROR_FORMS`; eine Zeile
  ohne Form gilt nicht als Spiegel), die Forderungsbuchung einer eigenen Rechnung (`_receivable_sql`, dieselbe Bedingung wie im Editor) und eine
  Buchung, an der – oder an einem ihrer Kinder – dieser Beleg schon hängt, gleich welcher Art. **Punkte** (`rank_candidates`, je Signal ein fester
  Satz als Begründung, der Betrag zuerst): Rechnungsnummer des Belegs im Text, in der Beleg-Nr. oder im Empfänger der Buchung (`POINTS_NUMBER` = 100;
  normalisiert mindestens 4 Zeichen mit einer Ziffer, rein numerisch erst ab 5), gemeinsame Namenswörter (30, ab zwei 40; mindestens 4 Zeichen, ohne
  `STOP_WORDS`), ein Buchungsdatum nahe dem Belegdatum (bis 20, je 3 Tage Abstand einer weniger). **Zahlungsrichtung** (`direction_fits`, nach `Kind` der
  Lesung): Eine Rechnung (oder ein Beleg ohne Art) erwartet einen Ausgang (Betrag der Buchung < 0), eine Gutschrift einen Eingang (> 0); passt die Richtung
  nicht, trägt der Vorschlag die Begründung `REASON_DIRECTION` und steht hinter denen mit passender Richtung – ausgeschlossen wird er nie (eine unbekannte
  Art oder ein Betrag, der keine Zahl ist, „passt“). Reihenfolge nach `(linked, mismatch, −Punkte, Abstand, ID)`: eine Buchung, die schon einen Beleg der
  Art `receipt` trägt (`linked`), immer zuletzt, innerhalb davon die mit unpassender Richtung hinter denen mit passender; **höchstens 5**
  (`MAX_SUGGESTIONS`) nach dieser Sortierung (`Suggestion.mismatch` dient nur der Reihenfolge, nicht der JSON-Form).
  `GET /receipts/suggestions?id=<n>` (`_SEARCH_PATHS`: rein lesend, Herkunft geprüft, `no-store`, ID über `_id_param`, unbekannter Beleg 404, ein
  unerwarteter Fehler 500 mit festem Satz) liefert `{suggestions: [{booking, reasons, linked}], empty}` in der Form eines Suchtreffers
  (`receipt_links.suggestions_payload`); `empty` ist der Satz für die leere Liste – EINE Funktion für Route und Seite
  (`pages_receipts.suggestions_empty_text`: verknüpft → `LINKED_TEXT`, kein Brutto oder keine Lesung → `NO_AMOUNT_TEXT`, sonst `NO_SUGGESTIONS_TEXT`), das
  Skript übernimmt ihn vor dem Zeichnen (`RECEIPT_SUGGEST.empty`). Dieselben Daten stehen im Skriptblock der Seite des Belegs (`RECEIPT_SUGGEST`, nur über `script_json`). Den
  Bereich „Vorschläge“ gibt es nur zu einer Lesung `ok` (`pages_receipts._suggestions_area`); scheitert die Abfrage dort, bleibt die Seite stehen
  (leere Liste, der Fehler im Server-Log). **„Zuordnen“ ist der bestehende Weg `POST /bookings/link-document` (*Belege verknüpfen*) – die Regeln der
  Beleg-Nr. gelten unverändert;** danach und nach dem Lösen holt das Skript die Vorschläge neu. Bekannte Grenzen: gesucht wird nur der Betrag auf den
  Cent (Skonto, Teilzahlung und Sammelüberweisung finden sich nicht) und nur in Euro (`docs/aufgaben.md`)
- **Tests** (nur mit erfundenen Belegen, Namen und Beträgen): `tests/test_receipt_reading_einvoice.py` (E-Rechnung, Grenzen der Eingabe, `Reading`),
  `tests/test_receipt_reading_text.py` (Textregeln, `pdf_text`, Kindprozess), `tests/test_document_readings_schema_2006.py` (Migration und
  DB-Schicht), `tests/test_receipt_read_route.py` (Route, Spalte, Kasten), `tests/test_receipt_matching.py` (Rangfolge, Kandidaten),
  `tests/test_receipt_suggestions_route.py` (Route und Bereich „Vorschläge“)
