# Kontoauszug-Import und WISO-Datenübernahme

## Kontoauszug-Import (CAMT.053 + PDF + CSV)

- Ein Weg für alle Formate: `importers/` liefern `BankStatement` (`base.py`) → `server/upload_handler.py` → Vorschau
  `server/import_preview.py` → bestätigen. Nur CAMT füllt die Zusatzfelder einer Bankbewegung (`importers.base.DETAIL_FIELDS`: Valuta, Bankreferenz,
  End-to-End-Referenz, Mandatsreferenz, Gläubiger-ID); `StatementTransaction.as_dict` nimmt sie nur auf, wo sie einen Wert haben (PDF, CSV und ältere
  Importdateien behalten ihre Schlüssel). Jeder Wert geht durch `clean_bank_text`/`clean_value_date`: Platzhalter der Bank, mehr als 35 Zeichen, Steuerzeichen
  und ein Datum, das es nicht gibt, bleiben leer. Der Import schreibt sie mit der Bankzeile nach `BankTransactionDetails` (`insert_booking(bank_details=…)`, EINE
  Transaktion); die Maske der Bankbewegung zeigt sie nur zum Lesen, kein Schreibweg der Masken fasst die Tabelle an.
- Ablage der Seite *Buchungen*: `#dropZone`/`#fileInput`/`#uploadStatus` stehen im Bereich, den `openEditForm` austauscht. Hörer
  an `document.body`, Elemente bei jedem Ereignis neu suchen; nie ein Hörer oder eine Konstante an diesen Elementen
  (`tests/test_statement_upload_listeners.py`).

### CAMT und PDF

- CAMT (`importers/camt.py`): Storno-Richtung = `CdtDbtInd`; Saldoprüfung exakt in Decimal/Minor Units; DTD/Entities abgelehnt
  (`importers/safe_xml.py`, `parse_untrusted_xml`, auch von der Belegerkennung genutzt); ZIP nur im Speicher mit Grenzen;
  Beträge ≥ 10¹² sind Fehler. Diese Schutzmaßnahmen nicht aufweichen.
- Nennt ein CAMT-Auszug irgendwo eine andere Währung als Euro: keine Umsätze, nur Befund `foreign_currency`
  (`_statement_currency`). **Nur `TxDtls/Amt` teilt eine Sammelbuchung auf** (`_tx_amount`), nie `AmtDtls` – nicht ändern, ohne
  die Duplikaterkennung mitzuziehen.
- Zusatzfelder (`_build`): Valuta `Ntry/ValDt`; Bankreferenz `Ntry/AcctSvcrRef` **nur auf Ebene des Umsatzes** (alle Teile einer Sammelbuchung teilen sie, und sie
  bleibt, wenn eine Sammelbuchung als ein Umsatz gelesen wird); End-to-End- und Mandatsreferenz aus `TxDtls/Refs`; Gläubiger-ID = erstes `Othr` mit Schema `SEPA`
  unter `RltdPties/Cdtr` (Form .02/.04 und .08, `_creditor_id`).
- Saldoprüfung für alle Formate: `pdftext.find_balances` (lineare Laufzeit), `plausibility.check_balance`; `balance` = stimmt
  nicht, `no_balance` = nicht gefunden – nichts wird geraten.
- Volksbank-PDF: Jahr nur aus dem Kopf (`header_year`, sonst `no_year`); Buchung nach `latest` → Vorjahr (`_booking_date`);
  nicht existierendes Datum = `None` (Befund `date`, Vorschau `incomplete`), nie das heutige; Buchungskopf ohne lesbaren Betrag
  = `unparsed`; Saldozeilen und Strichzeilen (U+2500–U+257F) gehören zu keiner Buchung. `daterange` meldet auch ein Datum mehr
  als 31 Tage NACH dem Belegdatum (`MAX_DAYS_AFTER_DOCUMENT`). Ein als Auszug erkanntes PDF ohne Umsätze ist eine Warnung, kein
  Beleg. Texte fest in `FILE_WARN_LABELS`.

### DKB-CSV (`importers/dkb_csv.py`)

- Zwei Formate, **erkannt an der Datei, nie konfiguriert**: Kopfzeile = erste Zeile mit erster Zelle `Buchungsdatum`/`Buchungstag`
  (Trennzeichen an ihr gezählt). Spalten nach Bedeutung (`MEANS`), nie nach genauem Namen; Pflicht: Betrag, Verwendungszweck,
  Gegenseite (beide Seiten oder eine Spalte, nie nur eine Seite). Andere Währung als Euro im Namen der Betragsspalte: abgelehnt.
- **Beträge nur über `money.parse_grouped_amount`:** Punkt ist IMMER Tausendertrenner (`-15.000` = −15000), bewusst anders als
  `parse_amount`; nicht angleichen, nie `float()`.
- Befunde: `no_balance` immer, `skipped_status` („Vorgemerkt …“), `unparsed`, `no_iban` (setzt `check_statement`). Still
  übergangen wird nur die Fußzeile ohne Datum UND ohne Betrag.
- **Eine verrutschte Zeile wird nie gelesen** (`unparsed`): mehr Zellen als die Kopfzeile (auch leere) oder zu wenige für Betrag,
  Zweck, Gegenseite. Datum vor 1990 (`MIN_YEAR`) oder mehr als 31 Tage nach heute: nicht lesbar. Nicht lockern.
- `document_date` bleibt `None` (kein `daterange`). Nur im Speicher gelesen (`upload_handler._collect_csv`). Grenzen: 20 MB,
  100 000 Zeilen, Kopfzeile in den ersten 100 Zeilen, 100 Spalten, 2000 Trennzeichen je Zeile (`_lines`);
  `csv.field_size_limit` nie umstellen. Keine DKB-Liste oder unlesbar: Warnung (`other_files`), nie ein Beleg. Meldungen
  (`DkbCsvError`) fest, ohne Feldwerte. Nur an erfundenen Beispielen geprüft.

### Einzelne Umsätze auswählen

- Alle Formate, nur bei gemischt genutztem Konto: `selectable` = Konto gefunden UND `Database.account_allows_selection` (nur die
  ganze Zahl 0 in `Accounts.BusinessOnly` = gemischt; **jeder andere Wert ist „reines Geschäftskonto“, die Kasse nie**;
  `db.accounts.is_business_only`). Vorschau nennt je Umsatz `index` (Position in `import_preview.file_transactions` – DIE eine
  Stelle; Vorschau und Import gehen dieselbe Liste in derselben Reihenfolge) und `importable`.
- `/confirm_transactions` nimmt `selection` (`{"<file_index>": [<index>, …]}`): Beleg mit Eintrag legt nur die genannten Umsätze
  an, ohne Eintrag ganz. **Streng vor dem ersten Schreiben geprüft** (`_import_selection`): kein JSON-Objekt, doppelter
  Schlüssel, Schlüssel ohne kanonische ASCII-Ganzzahl oder fremder `file_index`, Wert keine Liste ganzer Zahlen (kein `bool`) im
  Bereich der Umsätze, doppelte Positionen, Feld mehrfach → `400` „Ungültige Auswahl“; Eintrag für Beleg ohne Konto oder ohne
  erlaubte Auswahl → `400`. Nichts ist dann importiert.
- Duplikat-Zählung unverändert: Jede Zeile wird eingeordnet (`classify_duplicates` rechnet für alle Zeilen der Datei, gewählt oder
  nicht), dann Duplikat → `skipped`, unvollständig → `skipped_invalid`, nicht gewählt → `skipped_unselected`, sonst angelegt.

### Duplikate

- **Die Zahl der Duplikate** je Schlüssel (Konto + Buchungsdatum + Betrag) ist die Zählung, **ohne Text**: `n = min(Umsätze der Datei, nicht erzwungene Bestandszeilen)` (Bestand:
  `Database.bank_stock`, dieselbe Bedingung wie `check_booking_exists`), je Datei. Der Bestand enthält, was frühere Dateien desselben Laufs angelegt haben; über mehrere
  Dateien gilt das Maximum, nicht die Summe. Eine Zeile, die dieser Lauf mit `force` angelegt hat, zählt dafür nicht. „Alle importieren“ (`handle_confirm_import`) =
  Einzelimporte nacheinander; die Vorschau (`build_import_preview`) rechnet dasselbe ohne Schreiben. Die Zählung hat EINE Stelle, `import_preview.classify_duplicates` (Vorschau,
  Import und die Prüfung von `force`; Quelle `db` vor `upload`). **Die Bankreferenz ändert die Zahl nie:** Von selbst wird nie mehr angelegt, als die Zählung erlaubt; ein anderes
  Datum oder Konto gleicht nichts ab.
- **Welche Umsätze** die Duplikate sind, bestimmt die Bankreferenz, wo beide Seiten eine tragen: (1) Ein Umsatz mit Referenz belegt die erste freie Bestandszeile mit genau dieser
  Referenz (auch eine erzwungene) – er ist ihr Duplikat. (2) Die übrigen Umsätze in Dateireihenfolge, solange die Zahl nicht erreicht ist: mit Referenz die erste freie Zeile
  ohne, ohne Referenz zuerst eine mit, dann eine ohne (nur nicht erzwungene Zeilen). (3) Fehlt dann noch etwas an `n`, sind die ersten nicht zugeordneten Umsätze Duplikate
  „nur nach Zählung“. Alles Übrige ist neu. Ohne Referenzen (auf einer Seite) sind die Duplikate die ersten `n` Umsätze, wie die Zählung nach Position sie ergibt; mit
  Referenzen auf beiden Seiten kann eine andere Zeile das Duplikat sein (Bestand A, Datei B, A: A ist das Duplikat, B wird angelegt). `dup_source` ist die Quelle der belegten
  Zeile, bei „nur nach Zählung“ die der ersten freien nicht erzwungenen.
- **Verdächtig** (`ref_differs`/`forceable`, Befund `ref_differs`) sind genau die Umsätze „nur nach Zählung“: Sie tragen eine eigene Referenz, und keine Bestandszeile passt
  („gleiches Datum und gleicher Betrag, aber andere Bankreferenz als die vorhandene Bewegung“). Ohne Referenz ist nie ein Umsatz verdächtig (PDF, CSV, Bestand ohne Details). Er bleibt
  `dup` und nicht `importable`; die Karte bietet je solcher Zeile „trotzdem importieren“ (nie vorab angehakt, auch bei reinem Geschäftskonto – dort steht die Liste dann offen).
- `/confirm_transactions` nimmt `force` (`{"<file_index>": [<index>, …]}`, `_import_force`, so streng wie `selection`): Jede Position muss in der frisch gerechneten Einordnung der
  Belege dieses Laufs verdächtig sein (`_force_refusal`, rechnet wie die Vorschau und kennt keine erzwungene Zeile), sonst `400` vor dem ersten Schreiben: „Ungültige Auswahl“ bei einem
  Formfehler oder einem Beleg, den dieser Lauf nicht anlegt; „Die Vorschau ist nicht mehr aktuell – bitte verwerfen und die Datei erneut hochladen.“ (`STALE_PREVIEW`) bei einer Position,
  die nicht (mehr) verdächtig ist. Der Import prüft jede Position beim Schreiben noch einmal gegen seinen Bestand (eine nicht mehr verdächtige bleibt ein gewöhnliches Duplikat).
  Erzwungen = `inserted` und `forced` (Schlüssel nur bei Treffern), unabhängig von `selection`; die Zeile legt genau sich selbst zusätzlich an (kein als „neu“ gezeigter Umsatz
  verdoppelt eine vorhandene Bewegung), zählt für spätere Belege des Laufs nicht für die Zahl, trifft aber bei gleicher Referenz, und ist beim nächsten Import ein gewöhnliches Duplikat
  mit gleicher Referenz. Beim Einzelimport ist sie Bestand wie jede andere Zeile. Ohne `force` entscheidet allein die Zählung die Zahl (`tests/test_import_bank_refs.py`).
- Der Text-Backfill (`backfill_import_text`) überschreibt nie den Text einer Zeile, die derselbe Beleg gerade angelegt hat oder die ein `force` angelegt hat (`protected`); sie zählt aber wie
  jede passende Zeile für die Mehrdeutigkeit. Was ein früherer Beleg desselben Laufs angelegt hat, lässt sich weiter verlängern (kurzer Text zuerst, langer danach).
- Grenzen: CAMT-Sammelbuchung gegen PDF-Summenzeile (die PDF-Seite hat keine Referenz); zwei verschiedene Auszüge desselben Tags mit je einem echten, gleichen
  Umsatz, wenn nicht beide eine Bankreferenz tragen (mit verschiedenen: verdächtig, erzwingbar). Ein Umsatz ohne Referenz (PDF, CSV) lässt sich nicht erzwingen.
- `text_differs` (Duplikat aus anderer Datei mit nicht passendem Verwendungszweck): nur Hinweis.
- Umsatz ohne Datum oder Betrag: in Vorschau und Import gleich nicht importierbar (`incomplete`, `skipped_invalid`). Betrag 0,00 €
  ist kein Befund: gewöhnlicher Umsatz (`tests/test_import_zero_amount.py`); als Bankbewegung ohne Buchungssatz nicht „offen“
  (siehe *Dashboard*).

## WISO-Datenübernahme

- Zwei Wege: **Textexporte** (`db/wiso_import.py`, CSV) und **WISO-Datenbank** (`db/wiso_fdb_import.py` +
  `importers/wiso_fdb/`; Firebird-Dateien `DB1.FDB` Mandant, `DB0.FDB` Standard-Kontenrahmen, ohne Firebird-Installation gelesen).
  `ACCOUNTINGID` (→ `Bookings.SourceGroup`) nennt Teilbuchungen ausdrücklich.
- **Klammer eines Belegs ist Beleg-Nr. + Datum**, nicht die `SourceGroup` (feiner, greift nur, wo eine Beleg-Nr. fehlt).

### Satzversionen (`importers/wiso_fdb/ods.py`)

- `OdsFile.rows` liefert je Satz den letzten **abgeschlossenen** Stand, nicht die oberste Version. Zustand aus den TIP-Seiten
  (2 Bit je Transaktion, 3 = abgeschlossen; Transaktion 0 gilt als abgeschlossen); zurück über die Rückzeiger; `R_DELTA` →
  Vorversion ist Differenzstrom (`apply_diff`). Kein Rückzeiger oder abgeschlossen gelöscht: Satz gibt es nicht.
- Gezählt, nichts verschwiegen: `version_stats` → `uncommitted_records`, `unreadable_versions` (kein Blocker),
  `unknown_tx_state`. Tests: `tests/fixtures/firebird/`, `tests/fdb_builder.py`. Nicht aufweichen.

### Konten

- WISO rechnet in SKR03. SKR04-Nummer in dieser Reihenfolge (`WisoDatabase.chart()`): (1) `BAS_FINACC_PLAN.SKR04` (Mandant
  schlägt Standard; **nie überschrieben**), (2) von WISO gespeicherte Nummer (`FINT_EXTERNDEFS.EXTERNVALUE`, `EXTERNKIND = 82`,
  höchste `FINTABLEID`; nur `ACCOUNTNO` < 10000, Wert 1–9999; nicht für `KNOWN_EQUIVALENTS`), (3) `BASEACCOUNTNO` (nur
  Rückgriff), (4) `KNOWN_EQUIVALENTS`. Unterkonten behalten ihre Nummer. Konten ohne Zuordnung **gemeldet, nicht geraten**;
  fehlende SKR04-Konten legt der Import mit WISOs Bezeichnung an.
- Bestand mit Sätzen auf dem Basiskonto bleibt: Re-Import erkennt ihn an der Quellgruppe, zählt ihn (`subaccount_rows`);
  vorhandene Anlage behält ihr Konto. Unterkonto eines Zahlungskontos ohne Zahlungskonto-Rolle: `liquid_subaccounts`.

### Vorzeichen und Form

- Beide Importe richten das Vorzeichen an den liquiden Seiten aus (eigene Konten und 1460): genau eine liquide Seite →
  Richtung der Geldbewegung (negativer Brutto dort: `negative_gross`); keine oder zwei: Rohbetrag. Form: Konto liquide
  `liquid_first`, Gegenkonto liquide `purpose_first`, beide `mirror_coa` (**Rohbetrag**, positiv = Konto im Soll), **keine
  liquide Seite `transfer`** mit Rohbetrag (auch bei leerem Gegenkonto, nie `purpose_first`).
- **Kein Import ändert die Form einer vorhandenen Zeile.** Re-Import erkennt einen `mirror_legacy`-Spiegel wieder (gleiches oder
  umgekehrtes Vorzeichen; Beleg-Nr., Datum, Konto, Gegenkonto gleich; von der Datei nicht selbst beansprucht), legt keinen
  zweiten Satz an, lässt Form und Betrag, zählt ihn (`legacy_mirrors`). Andere abweichende Form: nur `form_conflicts`.
- Altzeilen auf Konten 1000–1099 (damals als Kasse gelesen) erkennen beide Wege am Betrag der alten Regel wieder (Textexport mit
  zusätzlicher Duplikatsuche; Übernahme CSV → Datenbank über `WisoBooking.legacy_amount`). Kein zweiter Satz; Zähler
  `legacy_stock_rows`.

### Textimport (`import_wiso_csv`)

- Kodierung: BOM → strenges UTF-8 → cp1252 (cp1252 nie zuerst).
- **Beträge aus Dateien nur über `money.parse_amount`** (nie `float()`; Ausnahme DKB): nur ASCII, `1.234,56` (Tausenderpunkte nur
  in Dreiergruppen) oder ohne Komma mit Punkt als Dezimalpunkt, höchstens 30 Zeichen, unter einer Billion (`MAX_PARSED_AMOUNT`),
  sonst „Ungültiger Betrag“. PDF-Auszüge (`importers/vbr.py`, `document_parser.py`) verlangen das Komma. `money._to_decimal`/
  `to_minor` lehnen NaN/Infinity ab.
- Steuerbetrag aus `_included_tax`: auf dem Brutto der Datei, Brutto × Satz / (1 + Satz), EINE Rundung auf den Cent – nichts
  vorher runden.
- Meldungen der Formaterkennung nennen **keine Feldwerte der Datei** (DATEV-Stapel am ersten Feld `EXTF`/`DTVF` erkannt, mit
  Klartext abgelehnt).
- `SCHLUESSEL` (1–9999) steht am Satz (`DatevKey`). Steuersatz zuerst aus `datev_keys.key_rate`, sonst aus `TaxKeys`. Unbekannter
  Schlüssel wird nicht geraten: Zeile ohne Satz, `unknown_tax_keys`. Die stille 19 % bei „4405 an 4400“ gilt **nur ohne Schlüssel**
  (leer oder 0). Der Import schreibt nur neue Zeilen – ein Duplikat bleibt.

### BU-Schlüssel und Nummern aus der WISO-Datenbank

- `DATEVVATKEY` (leer, 0, außerhalb 1–9999 = keiner) steht beim neuen Satz am Satz (`WisoBooking.datev_key`); CSV-Übernahme füllt
  ihn nur, wo er fehlt (`COALESCE`); Re-Import trägt ihn an der wiedererkannten Zeile bekannter Quellgruppe nach – **nur diese
  Spalte, nur wo leer, nur bei genauem Treffer** (`datev_keys_filled`) **und nur bei gleichem Steuersatz** (`_same_rate`). Sonst
  bleibt `DatevKey` leer (Export leitet ab), Zähler `datev_key_rate_mismatches`. Gleichen sich Zeilen in Datum, Konten, Betrag,
  bekommt der Satz zuerst die Zeile mit seinem Steuersatz.
- Berater-/Mandantennummer aus `SYS_FINACCSYSTEMS` (`NAME` = „Datev-Format“; **nur** `NAME`, `CONSULTANT_NO`, `CLIENT_NO` lesen –
  die Tabelle trägt Zugangsdaten anderer Schnittstellen; nie loggen). Sie gehen nur in die eigene Firma, wenn deren beide Felder
  leer sind (`datev_numbers_set`), **erst nachdem die Buchungen geschrieben sind**. Keine Firma, fehlende Tabelle/Spalte,
  Lesefehler (`datev_numbers_unreadable`): Hinweis ohne Nummern/Fehlertext, nie ein Abbruch.

### CSV und Datenbank nacheinander

- `_insert_wiso_bookings` übernimmt eine CSV-Buchung (`entry` ohne `SourceGroup`, `AutoMirror`, `Account_ID`) nur, wenn Datum,
  Betrag, Konto, Beleg-Nr. **und ein nicht leerer Text** gleich sind (Leerraum/`casefold`; WISO-Text latin-1 → cp1252) und das
  Gegenkonto nicht widerspricht; gleiches Gegenkonto vor kleinster ID; jede Zeile höchstens einmal.
- `mirror_legacy`-Spiegel mit umgekehrtem Vorzeichen: übernommen, wenn Konto, Gegenkonto (beide bekannt), Beleg-Nr., Text gleich
  und die Datenbank keine Zeile mit seinem Betrag enthält; Form und Betrag bleiben. Füllt die Übernahme ein fehlendes Konto, gilt
  die Form des Imports. Gleicher Schlüssel + Text bei verschiedenen Gegenkonten ist mehrdeutig → Zeile ohne Gegenkonto bleibt
  liegen; Ersatzsuche ohne Konto nur bei gleichem Gegenkonto und nicht, wenn sich zwei Sätze des Imports nur im Konto
  unterscheiden.
- Grundsatz: **im Zweifel neu anlegen und melden – nie aufweichen**; Ähnliches: `possible_duplicates`. Übernommen per UPDATE
  (Replay-Log; `adopted_rows`).

### Neu angelegte Sätze (Wiedererkennung)

- **Verwaist** = `entry` mit Quellgruppe, die in keinem Satz der Datei vorkommt (auch nicht in einem künftigen AfA-Satz). Der Import
  liest immer die ganze Datenbank, kein Zeitraumfilter – bekäme er einen, wäre die Logik abzuschalten.
- Jeder Satz mit **unbekannter**, nicht leerer Quellgruppe bekommt – vor der CSV-Übernahme und vor dem Neuanlegen – eine
  verwaiste Zeile, wenn eine passt. **Die Zuordnung steht fest, bevor die Hauptschleife läuft**, unabhängig von der
  Satzreihenfolge (`_REGROUP_PASSES`, feste Ordnung nach Inhalt, Zeilen nach ID); jede Zeile dient höchstens einem Satz und
  umgekehrt.
- **Immer nötig:** gleiches Datum, Konto, Gegenkonto (leer = leer) und **gleicher Steuersatz** (`_same_rate`). Dann nacheinander:
  1. gleicher Betrag mit Vorzeichen und gleiche nicht leere Beleg-Nr.; Text darf abweichen (`regrouped_text_differs`)
  2. gleicher Betrag, beide ohne Beleg-Nr., gleicher nicht leerer Text
  3. gleicher Betrag, Zeile ohne Beleg-Nr., Satz mit nachgetragener, gleicher nicht leerer Text
  4. nur Altbestand: Zeile `mirror_legacy` mit umgekehrtem Betrag, sonst wie 1–3, nur wenn kein Satz der Datei auf demselben
     Schlüssel (Datum, Konten, Beleg-Nr.) den Betrag der Zeile selbst trägt
- **Kein Rückfall auf den Absolutbetrag** für jede andere Form; Zeile mit Beleg-Nr. passt nie zu Satz ohne, zwei verschiedene
  Nummern nie.
- Ein Treffer ist **kein neuer Satz**: Nur `SourceGroup` wird gesetzt (UPDATE im Replay-Log), jede andere Spalte bleibt. Danach
  zählt die Zeile wie eine in bekannter Gruppe wiedererkannte (Weg `recognize`). Zähler `regrouped_rows`,
  `regrouped_text_differs`.
- **Verwaiste Zeilen ohne Treffer** meldet der Lauf (`orphaned_rows_count`, `orphaned_rows`) und die Seite *Daten*. Sie zählen in
  Auswertungen weiter mit, **gelöscht wird nichts**.
- **Eine Änderung innerhalb einer weiter bestehenden Buchungsgruppe bemerkt der Import nicht.**
- Grenzen: Die Suche braucht dieselben Konten (Basiskonto-Altbestand, Altzeilen auf 1000–1099: neu angelegt, Altzeile gemeldet,
  bis zum Bereinigen doppelt). Gedacht für EINE WISO-Datenbank je Bestand (anderer Mandant: nicht unterstützt). Neu gerechneter
  AfA-Plan unter neuer Gruppe zählt das Jahr bis zum Bereinigen doppelt.

### Upload und Bedienung (*Daten* → „Import aus der Datenbank“)

- **Dateien kommen als Upload (`server/wiso_upload.py`), einen Serverpfad nimmt der Import nicht entgegen:** `DB1.FDB` (Pflicht),
  optional `DB0.FDB`, roher Body über `POST /wiso/fdb-upload?slot=client|standard[&upload=<Kennung>]`; das Formular nennt danach
  nur die Kennung (`upload`).
- Ablage `<Datenordner>/upload_tmp/wiso_fdb/<Kennung>/` mit FESTEN Namen (Browser-Dateiname nie verwendet). Kennung
  (`secrets.token_hex(16)`, gegen `[0-9a-f]{32}` geprüft, Ordner direkt unter der Wurzel, kein Link) ist die einzige Eingabe, die
  einen Pfad bestimmt. Vor dem ersten gelesenen Byte: Token, Demo-Modus, Slot, Kennung, Länge, freier Platz. Schreiben in
  `*.part`, nach voller Länge umbenennen. Je Benutzer höchstens EIN wartender Upload (`begin`; Marker `.importing`).
- **`handle_wiso_fdb_import` löscht den Zwischenordner in jedem Fall** (`finally: discard`), und **kein Serverpfad geht in
  Antwort, Weiterleitung oder `wiso_fdb_result.json`** (`_without_upload_path`) – nicht aufweichen. `result['unreadable']` ist
  ein Fehler der Seite. `WisoDatabase.check` bricht vor dem Schreiben ab, wenn Tabellen/Spalten abweichen.
- **Gedacht für einen leeren Bestand:** Ist `Bookings` nicht leer (`Database.wiso_import_stock`), zeigt das Formular Hinweis und
  Pflicht-Kästchen (`confirm_existing`, genau `1`); `handlers.handle_wiso_fdb_import` prüft das vor jedem Zugriff auf die Datei
  und importiert sonst nichts. `Database.import_wiso_fdb` selbst prüft nichts.

### Anlagen

- Anschaffungswert und Verkaufserlös stehen **netto** in den Büchern. Der Erlös trägt in WISO keine `INVENTORYID`; nicht
  eindeutig über den Abgangstext → Feld leer.
- **Anschaffungswert** (`_purchase_price`): Summe der positiven Zahlungen plus `PURCHASEAMOUNTNET`, wenn über 1,00 € und nicht auf
  den Cent gleich der Zahlungssumme. Zählt er zusätzlich, warnt der Import je Anlage; Rest über 1,00 € bei nicht abgegangener
  Anlage ohne geplantes Jahr: `depreciation_rest_warnings`.

### Vorausgeplante AfA

- **Ein AfA-Satz mit Datum nach dem Importtag wird nie als Buchung angelegt** (`today`; `future_depreciations_skipped`). AfA-Satz
  = `INVENTORYID` und `INVENTORYAMORTID` oder `ACCOUNTINGKIND = 28`; Spalte da und leer, Art nicht 28 → Abgangsbuchung; kennt der
  Satz die Spalte nicht: gleiche Anlage, Datum, Betrag einer Zeile der AfA-Tabelle. Anlage nur über eindeutige Inventarnummer,
  sonst Warnung.
- **Der Planstatus folgt der Buchung, nicht dem Datum:** `AssetDepreciations`-Jahr ist `booked`, wenn sein AfA-Satz in den Büchern
  steht, sonst `planned`. `_insert_wiso_bookings` führt den Status in seiner Transaktion mit (`BEGIN IMMEDIATE`; auch bei
  `with_assets=False`), `_insert_wiso_assets` legt neue Planzeilen nach derselben Menge an, hebt/senkt sonst nie. Buchungen vor
  Anlagen. Hat die App ein Jahr selbst gebucht (`Booking_ID`), bleibt die Planzeile (`depreciation_conflicts`).
- **Bekannte Quellgruppe:** nie neu anlegen – Ausnahme: Gruppe hat für das Jahr keine Zeile und die eindeutig zugeordnete Anlage
  führt es als `planned` → nachtragen. Nicht genau passende Zeile: Jahr gilt als gebucht. Künftige AfA aus älterem Import bleibt
  (`future_depreciations_kept`).
- Planzeile, die `booked` wird, trägt den **Betrag der Bücher** (Wirkung der AfA-Sätze auf das Anlagekonto, nie unter 0); der
  WISO-Planwert aktualisiert nur `planned`. Abweichung des App-Plans (`depreciation_plan`) über 1,00 €:
  `depreciation_plan_mismatches`. Nicht aufweichen – sonst doppelte oder fehlende AfA.

### Rechnungen

- Positionstext: RTF-Blob (`ARTDESCR`) an der Auftragsposition, auf die `ORDPOSID` zeigt. Einheiten: UN/ECE-Code aus
  `SUP_ARTICLES_UNITS.OPENTRANSCODE`. Kunden behalten ihre WISO-Nummer; Sammelposten („(alle)“, nicht-positive Id) sind keine
  Kunden.
