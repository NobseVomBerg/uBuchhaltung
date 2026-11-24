# Kontoauszug-Import und WISO-Datenübernahme

## Kontoauszug-Import (CAMT.053 + PDF + CSV)

- Ein Weg für alle Formate: Parser in `importers/` liefern `BankStatement` (`base.py`) →
  `server/upload_handler.py` → Vorschau `server/import_preview.py` (Duplikate, Warnungen) → bestätigen
- **Die Ablage der Seite *Buchungen* (seit 2.6.9):** `#dropZone`, `#fileInput` und `#uploadStatus` stehen in dem Bereich, den `openEditForm`
  austauscht – nach dem Öffnen einer Buchung sind es neue Elemente. Ihre Hörer hängen deshalb an `document.body` (nicht an `document`: die
  Schleife der Zieh-Ereignisse hält sie dort an) und suchen die Elemente bei jedem Ereignis neu; nie eine Konstante oder ein Hörer an diesen
  Elementen (`tests/test_statement_upload_listeners.py`)
- CAMT (`importers/camt.py`): Storno-Richtung = `CdtDbtInd` (ISO 20022); Saldoprüfung exakt in
  Decimal/Minor Units, nie als float-Summe; DTD/Entities werden abgelehnt (der XML-Wächter liegt seit 2.6.0 in
  `importers/safe_xml.py`, `parse_untrusted_xml`; die Belegerkennung nutzt dieselbe Stelle), ZIP nur im Speicher mit
  Grenzen, Beträge ≥ 10¹² sind Fehler – diese Schutzmaßnahmen nicht aufweichen
- **Nichts still falsch oder unvollständig (seit 2.3.9):** Ein CAMT-Auszug, der irgendwo (Konto, gebuchter Umsatz, verwendeter
  Saldo) eine andere Währung als Euro nennt, liefert keine Umsätze und nur den Befund `foreign_currency`
  (`_statement_currency`, `BankStatement.currency`); ein Einzelbetrag einer Sammelbuchung in anderer Währung teilt nicht auf.
  **Nur `TxDtls/Amt` teilt eine Sammelbuchung auf** (`_tx_amount`), die Beträge unter `AmtDtls` bewusst nicht: So las der
  Import solche Auszüge immer, und ein erneuter Import fände seine Summenzeile sonst nicht wieder – nicht ändern, ohne die
  Duplikaterkennung mitzuziehen. **Saldoprüfung für alle Formate:** `pdftext.find_balances` (Zeilen „alter/neuer
  Kontostand“, lineare Laufzeit – der Text kommt aus hochgeladenen PDFs) und `plausibility.check_balance` (ganze Cent);
  `balance` = stimmt nicht, `no_balance` = Saldo nicht gefunden (auch CAMT ohne OPBD/CLBD) – nichts wird geraten.
  **Volksbank-PDF:** Das Jahr steht nur im Kopf (`header_year`, fehlt es: `no_year`); läge eine Buchung damit nach dem
  Erstellungsdatum (`latest` = „erstellt am“), gilt das Vorjahr (`_booking_date`); ein Datum, das es nicht gibt, ist `None`
  (Befund `date`, in der Vorschau `incomplete`) – nie das heutige. Ein Buchungskopf ohne lesbaren Betrag ergibt `unparsed`,
  Saldozeilen landen nicht im Verwendungszweck. `daterange` meldet auch ein Datum mehr als 31 Tage NACH dem Belegdatum
  (`MAX_DAYS_AFTER_DOCUMENT`). Die Belegwarnungen eines PDF gehen in die Vorschau (`upload_handler`), ein als Auszug
  erkanntes PDF ohne Umsätze wird als Warnung gemeldet statt als abgelegter Beleg. Die Texte stehen fest in
  `FILE_WARN_LABELS` (`server/pages_transactions.py`). Die Saldosuche ist an 55 echten
  Volksbank-Auszügen eines Kontos (2022–2026) bestätigt: beide Saldozeilen überall gefunden, die Rechnung geht auf den Cent
  auf, kein Befund. Nicht gemessen: Soll-Saldo, einseitiger Auszug, Januar-Auszug mit Dezember-Buchungen (`docs/aufgaben.md`).
  Reine Strichzeilen (Rahmenzeichen U+2500–U+257F, seit 2.3.13) gehören zu keiner Buchung – neuere Auszüge rahmen „neuer
  Kontostand“ damit ein
- **DKB-CSV (seit 2.5.0, `importers/dkb_csv.py`):** Die Umsatzliste der DKB, zwei Formate – neu (`Buchungsdatum`, Gegenseite in zwei
  Spalten, Datum `TT.MM.JJ`, UTF-8 mit BOM) und alt (`Buchungstag`, eine Spalte, `TT.MM.JJJJ`, oft Windows-1252, auch mit Komma statt
  Semikolon und mit verlorenem Umlaut) –, **erkannt an der Datei, nie konfiguriert**: Kopfzeile = erste Zeile, deren erste Zelle
  `Buchungsdatum`/`Buchungstag` heißt (ihr Trennzeichen wird an ihr gezählt, nicht an den Beträgen); die Spalten werden **nach
  Bedeutung** gefunden (`MEANS`), nie am genauen Namen oder an einem Umlaut – Pflicht sind Betrag, Verwendungszweck und Gegenseite
  (beide Seiten oder eine einzelne Spalte, nie nur eine der beiden Seiten); die Liste einer anderen Bank mit derselben Kopfzeile
  wird genauso gelesen, eine andere Währung als Euro im Namen der Betragsspalte abgelehnt. **Beträge nur über `money.parse_grouped_amount`:** der
  Punkt ist IMMER ein Tausendertrenner (die DKB lässt bei runden Beträgen die Nachkommastellen weg: `-15.000` ist −15000) – bewusst
  anders als `parse_amount` (`1.500` ist dort 1,5); Leerraum gilt nur am Rand und vor `€`/`EUR` (`12 34` ist kein Betrag); nicht
  angleichen, nie `float()`. Befunde am Beleg: `no_balance` immer (kein
  Anfangssaldo in der Datei), `skipped_status` (Status „Vorgemerkt …“ übergangen), `unparsed` (Datum oder Betrag nicht lesbar: nicht
  importiert; still übergangen wird nur die Fußzeile – kein Datum UND nichts in der Betragsspalte), `no_iban` (setzt
  `check_statement`; die IBAN steht im Vorspann). **Eine verrutschte Zeile wird nie gelesen** (`unparsed`): mehr Zellen als die
  Kopfzeile – auch leere, ein Trennzeichen ohne Anführung schiebt sonst die Bankleitzahl in die Betragsspalte – oder zu wenige für
  Betrag, Zweck und Gegenseite. Ein Datum vor 1990 (`MIN_YEAR`) oder mehr als 31 Tage nach heute gilt als nicht lesbar. Diese
  Prüfungen nicht lockern – ein falscher Betrag wiegt schwerer als eine Zeile, die der Nutzer nachträgt.
  `document_date` bleibt `None`: Ohne Belegdatum entfällt `daterange` – eine Umsatzliste hat keines, und jedes andere gäbe bei
  einem Jahresexport Fehlalarme. Nur im Speicher gelesen (`upload_handler._collect_csv`, nie auf die Platte): höchstens 20 MB, 100 000
  Zeilen hinter der Kopfzeile, Kopfzeile in den ersten 100 Zeilen, 100 Spalten, 2000 Trennzeichen je Zeile der Datei
  (`_lines`: bricht ab, bevor das csv-Modul Millionen Zellen anlegt); `csv.field_size_limit` wird nie umgestellt. Was keine
  DKB-Umsatzliste ist oder unlesbar bleibt, ist eine Warnung (`other_files`, `status: 'warning'`) und nie ein abgelegter Beleg; die
  Meldungen (`DkbCsvError`) sind feste Sätze und nennen **keinen Feldwert der Datei** (Spaltennamen der Kopfzeile höchstens auf 40
  Zeichen gekürzt). Nur an erfundenen Beispielen geprüft, nicht an einer echten Datei (`docs/aufgaben.md`)
- **Einzelne Umsätze auswählen (seit 2.5.0, alle Formate, nur bei einem gemischt genutzten Konto):** Die Vorschau nennt je Umsatz `index`
  (Position in `import_preview.file_transactions` – DIE eine Stelle, die die Liste liest; Vorschau und Import durchlaufen dieselbe
  Liste in derselben Reihenfolge) und `importable` (weder Duplikat noch unvollständig), je Beleg `selectable`. **Gekoppelt an das
  Kennzeichen am Bankkonto** (`Accounts.BusinessOnly`, Schema 2005): `selectable` = Konto gefunden UND
  `Database.account_allows_selection` (streng gelesen: nur die ganze Zahl 0 = gemischt genutzt; **Standard und jeder andere
  gespeicherte Wert – NULL, Text, 2 – ist „reines Geschäftskonto“, die Kasse nie**; `db.accounts.is_business_only`). Ohne Auswahl
  (reines Geschäftskonto) sieht die Karte aus wie vorher (Knopf „Alle Einzelbuchungen“, Liste zugeklappt); mit ihr steht die Liste beim Anzeigen aufgeklappt (Knopf „Umsätze auswählen“, er klappt sie weiter zu und auf), darin: Kästchen je Zeile (Standard: jede importierbare angehakt, Duplikate und
  unvollständige deaktiviert), Suchfeld, „Alle“/„Keine“ (nur auf sichtbare, auswählbare Zeilen), „Importieren (n ausgewählt)“, bei 0
  gesperrt. `/confirm_transactions` nimmt das Feld `selection` (JSON-Objekt `{"<file_index>": [<index>, …]}`): ein Beleg MIT Eintrag
  legt nur die genannten Umsätze an, einer OHNE wird ganz importiert. **Streng und vor dem ersten Schreiben geprüft** (`_import_selection`):
  kein JSON-Objekt, doppelter Schlüssel, ein Schlüssel, der keine kanonische ASCII-Ganzzahl und kein `file_index` des Imports ist, ein Wert, der
  keine Liste ganzer Zahlen (kein `bool`, keine Kommazahl) im Bereich der Umsätze ist, doppelte Positionen, das Feld mehrfach → `400`
  „Ungültige Auswahl“; ein Eintrag für einen Beleg ohne gefundenes Konto oder ohne erlaubte Auswahl (auch für einen anderen Beleg
  desselben Imports) → `400` „Für ein reines Geschäftskonto wird der Auszug vollständig importiert. Bitte die Vorschau verwerfen
  und die Datei erneut hochladen.“. Nichts ist dann importiert. **Die
  Duplikat-Zählung bleibt, wie sie war:** Jede Zeile wird eingeordnet (`dup_seen`/`dup_db_counts` rücken für jede Zeile vor, gewählt
  oder nicht), danach Duplikat → `skipped`, unvollständig → `skipped_invalid`, nicht gewählt → `skipped_unselected`, sonst angelegt;
  ein gewähltes Duplikat wird nie doppelt angelegt, der Text-Backfill bleibt. Der Beleg gilt danach als importiert (nicht gewählte
  Umsätze sind verworfen). Der Client schickt `selection` nur für Karten mit Kästchen
- Valuta, EndToEndId, Mandatsreferenz, Gläubiger-ID haben im Modell noch keinen Platz (Schemaänderung offen)
- **Duplikate** = Konto + Buchungsdatum + Betrag, ohne Text (`check_booking_exists`: CAMT und PDF
  schreiben den Text verschieden, der WISO-Tabellenexport überschreibt ihn), zählbasiert **je Datei**:
  Der Bestand enthält, was frühere Dateien desselben Laufs angelegt haben – über mehrere Dateien gilt
  das Maximum, nicht die Summe. „Alle importieren“ (`handle_confirm_import`) entspricht damit den
  Einzelimporten nacheinander; die Vorschau (`build_import_preview`: `dup_source` = `db`/`upload`)
  rechnet dasselbe, ohne zu schreiben. Grenzen ohne Bankreferenz im Modell: CAMT-Sammelbuchung gegen
  PDF-Summenzeile; zwei verschiedene Auszüge desselben Tags mit je einem echten, gleichen Umsatz.
  Die Vorschau kennzeichnet ein Duplikat aus einer anderen Datei, dessen Verwendungszweck (der Text, den
  der Import speichert) zu keinem Umsatz früherer Dateien passt (`text_differs`; passend = gleich, Anfang
  des anderen oder leer, verglichen ohne Leerraum, mit `casefold` und ohne führendes „Storno:“): nur ein
  Hinweis, der Import bleibt beim Maximum.
  Ein Umsatz ohne Datum oder Betrag ist in Vorschau und Import gleich behandelt – nicht importierbar
  (Befund `incomplete`; je Datei `skipped_invalid`), weder neu noch Duplikat; ein Betrag von 0,00 € ist dagegen kein Befund (`amount` heißt nur
  „Betrag fehlt“, also `None`): Die Zeile ist ein gewöhnlicher, importierbarer Umsatz und zählt bei den Duplikaten wie jede andere
  (`tests/test_import_zero_amount.py`); als Bankbewegung ohne Buchungssatz gilt sie nicht als offen (siehe *Dashboard*)

## WISO-Datenübernahme

- Zwei Wege: die **Textexporte** (`db/wiso_import.py`, CSV) und die
  **WISO-Datenbank selbst** (`db/wiso_fdb_import.py` + `importers/wiso_fdb/`)
- Die Firebird-Dateien (`DB1.FDB` = Mandant, `DB0.FDB` = Standard-Kontenrahmen)
  sind unverschlüsselt und werden **ohne Firebird-Installation** gelesen –
  reines Python, kein Treiber, keine DLL
- **Satzversionen (seit 2.3.10, `importers/wiso_fdb/ods.py`):** `OdsFile.rows` liefert je Satz den letzten **abgeschlossenen**
  Stand, nicht die oberste Version – war WISO beim Kopieren geöffnet oder wurde es nicht sauber beendet, kämen sonst geänderte
  Werte statt der gültigen, ein nur vorläufig gelöschter Satz fehlte, ein nie abgeschlossener neuer käme mit. Zustand einer
  Transaktion aus den TIP-Seiten (Seitentyp 3, über `RDB$PAGES`; 2 Bit je Transaktion, 3 = abgeschlossen; Transaktion 0 gilt
  als abgeschlossen – die Systemsätze tragen sie). Von der obersten Version über die Rückzeiger zurück bis zur ersten
  abgeschlossenen; trägt die neuere Version `R_DELTA`, ist die Vorversion ein Differenzstrom (`apply_diff`, nach dem
  RLE-Entpacken). Kein Rückzeiger oder abgeschlossen gelöscht: den Satz gibt es nicht. Gezählt wird, nichts verschwiegen
  (`version_stats` → Ergebnis `uncommitted_records`, `unreadable_versions` [Kette beschädigt: der Satz fehlt, kein Blocker],
  `unknown_tx_state` [TIP nicht lesbar: gelesen wie in der Datei]; feste Hinweise auf *Daten*). Geprüft gegen echte
  Firebird-3.0-Dateien mit offener Transaktion (`tests/fixtures/firebird/`, mit Firebird erzeugt – Fragmente, Blobs,
  Differenzketten); ungeprüft: `rhd_long_tranum` und mehrere TIP-Seiten an einer echten Datei. `tests/fdb_builder.py` schreibt
  eine TIP-Seite (Transaktion 1 abgeschlossen, 2 offen für `add_open`). Diese Regel nicht aufweichen
- Der Datenbankweg ist genauer: `MOV_FINACC_ACCRECORDS.ACCOUNTINGID` nennt die
  Zusammengehörigkeit von Teilbuchungen ausdrücklich (→ `Bookings.SourceGroup`),
  liefert die Anlagenverwaltung mit und umfasst alle Jahre
- WISO rechnet intern in **SKR03**. Die SKR04-Nummer eines Kontos kommt in dieser Reihenfolge (`WisoDatabase.chart()`, seit 2.2.5):
  `BAS_FINACC_PLAN.SKR04` (nur die alten Kontenpläne; Mandant schlägt Standard; **wird nie überschrieben**) → die von WISO
  **gespeicherte Nummer** (`FINT_EXTERNDEFS.EXTERNVALUE` bei `EXTERNKIND = 82`, jüngster Plan = höchste `FINTABLEID`; nur
  Sachkonten: `ACCOUNTNO` < 10000, Wert 1–9999; nicht für Konten aus `KNOWN_EQUIVALENTS`) → das Basiskonto
  (`FINT_ACCOUNTS.BASEACCOUNTNO`, nur noch Rückgriff) → `KNOWN_EQUIVALENTS`. Eigene Unterkonten (z. B. je Fahrzeug: 4532 → 6532)
  behalten so ihre Nummer – die Trennung ist fachlich nötig, „Basis + Abstand“ ist nicht WISOs Regel. Konten ohne Zuordnung
  werden **gemeldet, nicht geraten**; fehlende SKR04-Konten legt der Import mit WISOs Bezeichnung an. Ein Bestand aus einem
  Import bis 2.2.4 (Sätze auf dem Basiskonto) bleibt, wie er ist: Der Re-Import erkennt ihn an der Quellgruppe, legt keinen
  zweiten Satz an und zählt ihn (`subaccount_rows`); eine vorhandene Anlage behält ihr Konto (`COALESCE(COA_ID, ?)`). Ist das
  Unterkonto eines Zahlungskontos in der App kein Zahlungskonto, wird das gemeldet (`liquid_subaccounts`: gelesen als
  Umbuchung, nicht in der EÜR) – keine Sonderbehandlung
- **Klammer eines Belegs ist die Beleg-Nr. + Datum**, nicht die `SourceGroup`:
  WISO vergibt je Verrechnungskonto-Buchung eine eigene `ACCOUNTINGID`. Die
  Quellgruppe ist damit feiner und greift nur, wo eine Beleg-Nr. fehlt
- **Vorzeichen und Form:** Beide Importe richten das Vorzeichen an den liquiden Seiten aus (eigene Konten
  und 1460; genau eine Seite liquide → Richtung der Geldbewegung; ein negativer Brutto bei genau einer
  liquiden Seite zählt als `negative_gross`, keine oder zwei liquide Seiten: der Rohbetrag bleibt und
  zählt nicht) und schreiben die Form mit: Konto liquide `liquid_first`,
  Gegenkonto liquide `purpose_first`, beide `mirror_coa` (der **Rohbetrag**, positiv = Konto im Soll – der
  Betrag neuer Importe ändert sich dadurch nicht, ein Re-Import findet seine Zeilen), **keine liquide Seite
  `transfer`** mit dem Rohbetrag (WISO meint „Konto im Soll“ – auch bei leerem oder unbekanntem Gegenkonto,
  nie `purpose_first`; die EÜR liest so einen Satz als Umbuchung, ohne Vorsteuerausweis).
  **Kein Import ändert die Form einer vorhandenen Zeile:** Der Re-Import der Datenbank (bekannte `SourceGroup`)
  und der des Textexports erkennen einen `mirror_legacy`-Spiegel wieder – mit gleichem oder umgekehrtem
  Vorzeichen (Beleg-Nr., Datum, Konto, Gegenkonto gleich, von der Datei nicht selbst beansprucht) –, legen
  keinen zweiten Satz an und lassen Form und Betrag, wie sie sind; sie zählen ihn (`legacy_mirrors`, Hinweis
  auf *Daten*). Weicht eine andere gespeicherte Form von der des frischen Imports ab, wird das nur
  gemeldet (`form_conflicts`). **Zeilen auf Konten 1000–1099 aus 2.0.x** (dort als Kasse gelesen, also mit
  einem anderen Betrag gespeichert: „6815 an 1050“ −10, „1050 an 1800“ +30) erkennen beide Wege am Betrag der
  alten Regel wieder (Zahlungskonten einschließlich 1000–1099): der Re-Import des Textexports mit einer
  zusätzlichen Duplikatsuche (gleiche Beleg-Nr., Datum, Konto, Gegenkonto, jede Zeile höchstens einmal, nicht
  die, die eine andere Zeile der Datei mit ihrem eigenen Betrag beansprucht), die Übernahme CSV → Datenbank
  über `WisoBooking.legacy_amount` unter denselben Schutzregeln wie sonst. Kein zweiter Satz, Form und Betrag
  der Altzeile bleiben, die Übernahme füllt nichts; der Zähler heißt `legacy_stock_rows` (Hinweis auf
  *Daten*)
- **Textimport** (`import_wiso_csv`): Kodierung in der Reihenfolge BOM → strenges UTF-8 → cp1252 (cp1252 dekodiert fast alles
  und darf deshalb nie zuerst stehen). **Beträge aus Dateien liest nur `money.parse_amount`** (seit 2.3.12; nie `float()`: das
  nähme `nan`, `inf`, `1e5`, `1_000` und Ziffern anderer Schriften an; Ausnahme: die DKB-CSV liest `money.parse_grouped_amount`, in der
  der Punkt immer ein Tausendertrenner ist – siehe *Kontoauszug-Import*): nur ASCII, deutsch `1.234,56` (Tausenderpunkte nur in
  Dreiergruppen) oder ohne Komma mit Punkt als Dezimalpunkt (`1.234` bleibt 1,234 – die bisherige Lesart), höchstens
  30 Zeichen und betragsmäßig unter einer Billion (`MAX_PARSED_AMOUNT`), sonst der Zeilenfehler „Ungültiger Betrag“. Der
  Betrag bleibt `Decimal`; der Steuerbetrag kommt aus `_included_tax`: exakt auf dem Brutto der Datei, Brutto × Satz /
  (1 + Satz), EINE kaufmännische Rundung auf den Cent – weder das Brutto noch ein Zwischenwert wird vorher gerundet (in
  Minor Units gerechnet und danach gerundet wiche er bei rund 0,4 % der Beträge um einen Cent ab). Die PDF-Auszüge (`importers/vbr.py`, `document_parser.py`) lesen ebenso und
  verlangen das Komma; ein nicht lesbarer Betrag ist dort `unparsed` bzw. „kein Betrag“. `money._to_decimal`/`to_minor`
  lehnen NaN und Infinity mit `ValueError` ab. Die Meldungen der Formaterkennung nennen **keine Feldwerte der Datei** (ein DATEV-Stapel trägt Berater- und
  Mandantennummer im Vorlauf; er wird am ersten Feld `EXTF`/`DTVF` erkannt und mit Klartext abgelehnt). `SCHLUESSEL` (ganze Zahl 1–9999) steht am
  Satz (`DatevKey`); der Steuersatz kommt zuerst aus der Tabelle der Regeln (`datev_keys.key_rate`: 401/101/121 Regelsatz, 402/102/122 ermäßigt,
  7/6/5/4 die anderen Sätze – im zweiten Halbjahr 2020 mit 16/5/19/7 %), sonst aus `TaxKeys` (2.3.0: 100, 120, 121 „Verwendung von Gegenständen“, 122
  ergänzt bzw. berichtigt, 490 ohne Satz). Ein Schlüssel, den beide nicht kennen, wird nicht geraten: Die Zeile kommt ohne Steuersatz und zählt
  in `unknown_tax_keys` (Hinweis auf *Daten*). Die stille 19 % bei „4405 an 4400“ gilt **nur ohne Schlüssel** (leer oder 0): Eine Zeile
  mit einem Schlüssel, auch einem ohne Satz (490), behält, was er sagt. Der Import schreibt nur neue Zeilen – ein Duplikat bleibt, wie es ist,
  auch ohne Schlüssel
- **BU-Schlüssel und Nummern aus der WISO-Datenbank:** `DATEVVATKEY` (optionale Spalte; leer, 0 und alles außer 1–9999 ist keiner) steht beim neuen Satz
  am Satz (`WisoBooking.datev_key`), die Übernahme einer CSV-Buchung füllt ihn nur, wo er fehlt (`COALESCE`), und der Re-Import trägt ihn an der
  wiedererkannten Zeile einer bekannten Quellgruppe nach – **nur diese Spalte, nur wo sie leer ist, nur bei genauem Treffer** (`datev_keys_filled`;
  eine vom Nutzer geänderte Zeile bekommt nichts, ein vorhandener Schlüssel bleibt) – **und nur, wenn die vorhandene Zeile denselben Steuersatz
  trägt wie der WISO-Satz** (`_same_rate`: Prozentzahl, kein Satz = 0 %; bei der Übernahme gilt der Satz, den die Zeile nach dem UPDATE trägt –
  es füllt ihn, wenn Satz und Steuerbetrag leer sind): Sonst bleibt `DatevKey` leer, der Export leitet ihn aus dem Satz der App ab, und der Fall
  zählt in `datev_key_rate_mismatches` (Hinweis auf *Daten*) – der Schlüssel gehört zu dem Satz, für den WISO ihn geschrieben hat, und
  der Nutzer kann ihn mit `update_booking_fields` geändert haben. Gleichen sich in einer Gruppe mehrere Zeilen in Datum, Konten und Betrag,
  bekommt der Satz zuerst die Zeile mit seinem Steuersatz: Ein Schlüssel hängt nie an der Zeile eines anderen Satzes. Berater- und Mandantennummer
  stehen in `SYS_FINACCSYSTEMS` (Satz `NAME` = „Datev-Format“; **nur** `NAME`, `CONSULTANT_NO`, `CLIENT_NO` – die Tabelle trägt auch Zugangsdaten
  anderer Schnittstellen; nichts davon wird geloggt) und gehen nur in die eigene Firma (Unternehmen), wenn deren beide Felder leer sind
  (`datev_numbers_set`) – **erst nachdem die Buchungen geschrieben sind** (scheitert der Import, bleiben die Stammdaten, wie sie waren); gibt es
  keine eigene Firma (als Unternehmen angelegt), nennt ein Hinweis, dass die Nummern nicht übernommen wurden (ohne sie zu nennen); fehlt Tabelle
  oder Spalte, passiert nichts (Hinweis, kein Blocker), und ein Lese- oder Dekodierfehler der Tabelle (`datev_numbers_unreadable`) gibt „keine
  Nummern“ und einen Hinweis ohne Fehlertext, nie einen Abbruch
- **CSV und Datenbank nacheinander:** Der Datenbank-Import (`_insert_wiso_bookings`) übernimmt eine CSV-Buchung
  (`entry` ohne `SourceGroup`, `AutoMirror`, `Account_ID`) nur, wenn Datum, Betrag, Konto, Beleg-Nr. **und ein
  nicht leerer Text** gleich sind (Leerraum/`casefold`; der latin-1 gelesene WISO-Text wird dafür nach cp1252 umgesetzt) und das
  Gegenkonto nicht widerspricht; gleiches Gegenkonto geht vor, dann die kleinste ID, jede Zeile höchstens einmal.
  Ein `mirror_legacy`-Spiegel mit umgekehrtem Vorzeichen wird übernommen, wenn Konto, Gegenkonto (beide bekannt),
  Beleg-Nr. und Text gleich sind und die Datenbank nicht selbst eine Zeile mit seinem Betrag enthält; Form und
  Betrag bleiben (nur die Quellgruppe kommt dazu). Füllt die Übernahme ein fehlendes Konto, ändern sich die
  Konten des Satzes – dann gilt für ihn die Form des Imports.
  Gleicher Schlüssel + Text bei verschiedenen Gegenkonten ist mehrdeutig → eine Zeile ohne Gegenkonto bleibt liegen.
  Grundsatz: **im Zweifel neu anlegen und melden – nie aufweichen**; ähnliche, nicht übernommene Buchungen stehen
  als `possible_duplicates` auf der Seite. Übernommen wird per UPDATE (im Replay-Log; `adopted_rows`); die
  Ersatzsuche ohne Konto nur bei gleichem Gegenkonto – und nicht, wenn sich zwei Sätze des Imports nur im Konto
  unterscheiden
- **Neu angelegte Sätze (seit 2.3.2):** Legt WISO Buchungen neu an (nach einem DATEV-Export oder einer Korrektur), stehen sie mit
  neuer `ACCOUNTINGID` und sonst gleichem Inhalt in der Datenbank – die alte Quellgruppe gibt es dort nicht mehr. **Verwaist** ist
  eine `entry` mit Quellgruppe, die in keinem Satz der Datei vorkommt (auch nicht in einem künftigen AfA-Satz, dessen Gruppe die Datei
  ja noch nennt); der Import liest immer die ganze Datenbank, es gibt keinen Zeitraumfilter – bekäme er einen, wäre jede Zeile
  außerhalb verwaist, und die Logik gehörte dort abgeschaltet. Jeder Satz mit **unbekannter**, nicht leerer Quellgruppe bekommt – vor
  der Übernahme aus dem CSV-Import und vor dem Neuanlegen – eine verwaiste Zeile, wenn eine passt. **Die Zuordnung steht fest, bevor die
  Hauptschleife läuft** (Abbildung Satz → Zeile, die Schleife schlägt nur nach) und **hängt nicht von der Reihenfolge der Sätze in der
  Datei ab**: Die Durchgänge (`_REGROUP_PASSES`) gehen über ALLE diese Sätze in fester Ordnung nach Inhalt, die Zeilen nach ID; jede
  Zeile dient höchstens einem Satz, jeder Satz bekommt höchstens eine Zeile. **Immer nötig:** gleiches Datum, Konto, Gegenkonto (leer =
  leer) und **gleicher Steuersatz** (`_same_rate`) – ein anderer Satz ist nie ein Treffer (der Satz wird neu angelegt, die alte Zeile
  bleibt verwaist). Dann nacheinander: (1) gleicher Betrag mit Vorzeichen und gleiche, nicht leere Beleg-Nr. (Leerraum zählt nicht), der
  Text darf abweichen (`regrouped_text_differs`; die Zeile mit gleichem Text geht vor); (2) gleicher Betrag, beide ohne Beleg-Nr.,
  gleicher nicht leerer Text; (3) gleicher Betrag, Zeile ohne Beleg-Nr., Satz mit einer in WISO nachgetragenen, gleicher nicht leerer
  Text (die Beleg-Nr. der Zeile bleibt leer); (4) nur Altbestand: Zeile der Form `mirror_legacy` mit dem umgekehrten Betrag, sonst wie
  (1) bis (3) – und nur, wenn kein Satz der Datei auf demselben Schlüssel (Datum, Konten, Beleg-Nr.) den Betrag der Zeile selbst trägt
  (dieselbe Sperre wie bei der CSV-Übernahme). **Kein Rückfall auf den Absolutbetrag** für jede andere Form; eine Zeile mit Beleg-Nr.
  passt nie zu einem Satz ohne, zwei verschiedene Nummern nie. Ein Treffer ist **kein neuer Satz**: Nur `SourceGroup` wird gesetzt
  (UPDATE im Replay-Log, „neu angelegten Satz wiedererkannt“), jede andere Spalte bleibt – Betrag, Form, Text, Beleg-Nr., Steuer,
  `ParentBooking_ID`, Empfänger; der Grundsatz „Kein Import ändert die Form einer vorhandenen Zeile“ gilt unverändert. Die Zeile zählt
  für den Rest des Laufs wie eine in ihrer bekannten Gruppe wiedererkannte (derselbe Rückgabeweg wie `recognize`: `skipped`, AfA-Plan,
  `legacy_mirrors`/`form_conflicts`, BU-Schlüssel nur wo leer). Gezählt wird `regrouped_rows`, bei abweichendem Text zusätzlich
  `regrouped_text_differs`; die Zusammenfassung nach dem Import nennt sie (`regrouped`, `orphans` in der Weiterleitung; angezeigt nur, wenn > 0).
  **Verwaiste Zeilen ohne Treffer** meldet der Lauf (`orphaned_rows_count`, die ersten 50 in `orphaned_rows`: ID, Datum, Betrag,
  Kontonummern, Text) und die Seite *Daten* mit Links auf die Buchungen: Die Liste nennt Buchungen, deren Quelle (Quellgruppe)
  in der WISO-Datenbank nicht mehr steht – dort gelöscht oder mit anderem Inhalt neu angelegt. Sie zählen in den Auswertungen weiter mit,
  **gelöscht wird nichts**. **Eine Änderung innerhalb einer weiter bestehenden Buchungsgruppe (anderer Betrag, entfallene Teilbuchung)
  bemerkt der Import nicht: Die vorhandene Zeile bleibt, wie sie ist.** Grenzen: Die Suche braucht dieselben Konten (ein Bestand aus
  einem Import bis 2.2.4 auf dem Basiskonto eines Unterkontos findet keinen Treffer: neu angelegt, die Altzeile gemeldet; ebenso eine Zeile
  aus 2.0.x auf den Konten 1000–1099, die ihren Betrag nach der alten Kassenregel trägt – sie steht bis zum Bereinigen zweimal da). Der Import ist
  für EINE WISO-Datenbank je Bestand gedacht: Zeilen eines anderen Mandanten gelten als nicht mehr vorhanden und können bei gleichem
  Inhalt umgehängt werden – nicht unterstützt. Rechnet WISO den AfA-Plan eines schon importierten Jahres mit anderem Betrag unter neuer
  Gruppe neu, steht die alte AfA-Buchung in der Liste der nicht mehr vorhandenen Buchungen und die neue zusätzlich in den Büchern – bis
  zum Bereinigen zählt das Jahr doppelt. Die verwaisten Zeilen werden einmal gelesen und nach (Datum, Konto, Gegenkonto, Betrag) abgelegt (linear)
- Bedienung über *Daten* → „Import aus der Datenbank". **Die Dateien kommen als Upload (seit 2.3.7,
  `server/wiso_upload.py`), einen Serverpfad nimmt der Import nicht mehr entgegen:** Der Browser legt `DB1.FDB` (Pflicht) und
  optional `DB0.FDB` je als rohen Body über `POST /wiso/fdb-upload?slot=client|standard[&upload=<Kennung>]` ab, das Formular
  nennt danach nur die Kennung (`upload`). Die Dateien liegen unter `<Datenordner>/upload_tmp/wiso_fdb/<Kennung>/` mit FESTEN
  Namen (der Dateiname des Browsers wird nie verwendet); die Kennung (`secrets.token_hex(16)`, geprüft gegen
  `[0-9a-f]{32}`, Ordner direkt unter der Wurzel und kein Link) ist die einzige Eingabe, die einen Pfad bestimmt. Vor dem
  ersten gelesenen Byte stehen Token, Demo-Modus, Slot und Kennung, Länge und freier Platz; geschrieben wird in eine
  `*.part`-Datei und erst nach der vollen Länge umbenannt. Je Benutzer wartet höchstens EIN Upload (`begin` räumt die
  anderen weg, außer einem mit frischem Marker `.importing`). **`handle_wiso_fdb_import` löscht den Zwischenordner in jedem
  Fall** (`finally: discard` – nach dem Import, bei Blocker, Fehler und jeder Ablehnung), und **kein Serverpfad geht in
  Antwort, Weiterleitung oder `wiso_fdb_result.json`** (`_without_upload_path`; `result['file']` ist `DB1.FDB`) – beides
  nicht aufweichen. Eine Datei, die sich nicht als WISO-Datenbank öffnen lässt (`result['unreadable']`), ist ein Fehler
  der Seite, kein „Import abgeschlossen“. Die Prüfung
  (`WisoDatabase.check`) bricht vor dem Schreiben ab, wenn die WISO-Version
  andere Tabellen/Spalten hat. **Der Import ist für einen leeren Bestand gedacht:** Ist `Bookings` nicht leer
  (`Database.wiso_import_stock`: `bookings` = alle Zeilen, `wiso_rows` = `entry` mit Quellgruppe), zeigt das Formular über dem
  Absenden einen Hinweis (ohne bzw. mit Zeilen aus einem früheren Import der WISO-Datenbank) und ein Pflicht-Kästchen
  (`confirm_existing`, Wert genau `1`); `handlers.handle_wiso_fdb_import` prüft das vor jedem Zugriff auf die Datei und importiert
  ohne Bestätigung nichts („Der Bestand ist nicht leer – bitte den Import ausdrücklich bestätigen.“ über den Fehlerweg der Seite).
  `Database.import_wiso_fdb` selbst prüft nichts (Tests, Demodaten und Skripte rufen es direkt auf)
- **Anlagen:** Anschaffungswert und Verkaufserlös stehen beide **netto** in den
  Büchern. Der Erlös trägt in WISO keine `INVENTORYID` – gemeinsam ist ihm mit
  dem Anlagegut nur der Abgangstext; ist er nicht eindeutig, bleibt das Feld
  leer statt falsch. **Anschaffungswert** (`_purchase_price`, seit 2.2.6): Summe der positiven Zahlungen plus der in WISO
  eingetragene Wert (`PURCHASEAMOUNTNET`), wenn er über 1,00 € liegt und nicht auf den Cent der Zahlungssumme gleicht – bis
  1,00 € ist er ein Behelf des Nutzers bei der Erstanlage und zählt nicht, bei Gleichheit ist dieselbe Anschaffung zweimal
  erfasst und zählt einmal; ohne Zahlungen wie bisher. Zählt der eingetragene Wert zusätzlich, warnt der Import je Anlage
  (Zusammensetzung); bleibt bei einer nicht abgegangenen Anlage ohne geplantes Jahr nach den gebuchten Jahren ein Rest über
  1,00 €, warnt er ebenfalls (`depreciation_rest_warnings`)
- **Vorausgeplante AfA (seit 2.2.5):** WISO schreibt den ganzen AfA-Plan sofort als Buchungssätze, ohne Kennzeichen für
  „geplant“. **Ein AfA-Satz mit Datum nach dem Importtag wird nie als Buchung angelegt** (Stichtag `today`, Standard: Ortsdatum;
  `future_depreciations_skipped`) – gebucht wird im jeweiligen Jahr in der App. AfA-Satz = Satz mit `INVENTORYID` und
  `INVENTORYAMORTID` oder `ACCOUNTINGKIND = 28` (gemessen an einer WISO-Version); Spalte da und leer, Art nicht 28 → kein
  AfA-Satz (Abgangsbuchung); kennt der Satz die Spalte nicht, zählt gleiche Anlage, gleiches Datum und gleicher Betrag einer
  Zeile der AfA-Tabelle. **Der Planstatus folgt der Buchung, nicht dem Datum:** Ein Planjahr (`AssetDepreciations`) ist
  `booked`, wenn sein AfA-Satz in den Büchern steht (angelegt, aus CSV übernommen oder in der bekannten Quellgruppe
  wiedererkannt), sonst `planned`; `_insert_wiso_bookings` führt den Status vorhandener Planzeilen in seiner Transaktion mit
  (`BEGIN IMMEDIATE`, die Planjahre werden darin gelesen; auch bei `with_assets=False`), `_insert_wiso_assets` legt neue
  Planzeilen nach derselben Menge an, hebt sonst nie und senkt nie. Reihenfolge im Lauf: Buchungen vor Anlagen. **Hat die App
  ein Jahr selbst gebucht** (`Booking_ID` zeigt auf eine vorhandene Buchung), bleibt die Planzeile unverändert und der AfA-Satz
  der Datenbank wird nicht angelegt (`depreciation_conflicts`). **Bekannte Quellgruppe:** Ein Satz wird nie neu angelegt – mit
  einer Ausnahme: Fehlt der Gruppe für das Jahr jede Zeile und führt die eindeutig zugeordnete Anlage das Jahr als `planned`,
  wird der AfA-Satz nachgetragen. Hat die Gruppe im Jahr eine Zeile, die nicht genau passt (vom Nutzer geändert), gilt das
  Jahr als gebucht (Nachweis; jede Zeile für höchstens einen Satz, genaue Treffer zuerst). Künftige AfA-Buchungen aus einem
  Import bis 2.2.4 bleiben und zählen als gebucht (`future_depreciations_kept`). Zugeordnet wird die Anlage über die
  Inventarnummer, nur wenn sie eindeutig ist – sonst Warnung, kein Abgleich. Eine Planzeile, die der Buchungsschritt auf
  `booked` hebt oder die neu als `booked` entsteht, trägt den **Betrag der Bücher** (Wirkung der AfA-Sätze des Jahres auf
  das Anlagekonto, nie unter 0); der Planwert aus WISO aktualisiert nur `planned`-Zeilen, eine `booked`-Zeile gleicht allein
  der Buchungsschritt an den Betrag der Bücher an. Weicht der Plan der App (`depreciation_plan`) in einem geplanten Jahr um
  mehr als 1,00 € von WISOs Wert ab, meldet der Import es je Anlage (`depreciation_plan_mismatches`). Diese Regeln nicht aufweichen – jede Lockerung führt zu doppelter oder
  fehlender AfA
- **Rechnungen:** der Positionstext steht **nicht** bei der Rechnungsposition,
  sondern als RTF-Blob (`ARTDESCR`) an der Auftragsposition, auf die
  `ORDPOSID` zeigt – dort hat der Nutzer ihn für den Beleg angepasst.
  Einheiten kommen als UN/ECE-Code aus `SUP_ARTICLES_UNITS.OPENTRANSCODE`.
  Kunden behalten ihre WISO-Nummer; Sammelposten („(alle)“) tragen eine
  nicht-positive Id und sind keine Kunden
