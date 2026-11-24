# Buchungssätze, Stammdaten, Masken, Abgleich, AfA

## Stammdaten: Löschen, Standardkonten, Testdaten

- **Gelöscht wird nur, worauf nichts verweist:** `delete_contact`, `delete_article`, `delete_account`, `delete_asset_category`
  und `delete_receipt` prüfen die Verweise und löschen in EINER Transaktion (`BEGIN IMMEDIATE`); gibt es welche, werfen sie
  `db.DeleteRefused` (Klartext mit Zählern, Baustein `db.core.reference_texts`), und nichts ist gelöscht. Das gilt auch für
  Verweise, die das Schema per `ON DELETE CASCADE`/`SET NULL` mitnähme (Arbeitszeiten und Fahrten einer Person, der Beleg einer
  Fahrt): Eine Person löscht nie still ihr Fahrtenbuch. Kontakt: Rechnungen/Angebote (Kunde oder eigene Firma), Buchungen,
  Anlagen (Lieferant), zugeordnete Personen, Arbeitszeiten, Fahrten; Artikel: Positionen (mit dem Hinweis auf „inaktiv“, wenn er
  aktiv ist); Bankkonto: Buchungen, Rechnungen, Angebote – die Kasse nie; AfA-Kategorie: Anlagen; Beleg: Buchungen, Anlagen,
  Fahrten (`delete_receipt(receipt_id)` löscht genau EINEN Beleg über seine ID – die Belegnummer ist nicht eindeutig; Route
  `POST /receipts/delete?id=`, seine hochgeladene Datei geht mit, siehe *Belegdateien*; die Lesung der Datei, `DocumentReadings`, geht in derselben
  Transaktion mit und zählt nicht als Verweis, siehe *Belegerkennung*). Die Routen gehen über
  `_respond_after_delete` (`server/app.py`): 303 auf die Listenseite, die Meldung als
  Query-Parameter (`error` bei Artikeln, Bankkonten, Kontakten; `err` bei AfA-Kategorien und Belegen), angezeigt nur über
  `script_json` + `appMsg` – nie 500. Eine neue Verweisspalte auf eine dieser Tabellen gehört in die Prüfung ihrer
  Löschmethode. Jede Löschmethode schließt ihre Verbindung auch im Fehlerfall (try/rollback/finally) – sonst bleibt die
  Datenbank gesperrt
- **Testdaten lädt die Oberfläche nicht (seit 2.3.9):** Es gibt keinen Knopf und keine Route mehr (`POST /setup/load_testdata`
  antwortet wie ein unbekannter Pfad), weder unter *Daten* noch in der Ersteinrichtung – Beispieldaten gibt es im
  Demo-Modus, der sie beim Start selbst lädt. `Database.load_test_seed_data` bleibt für Demo-Modus und Tests
- **Standardkonten ergänzt nur der Nutzer:** `_seed_chart_of_accounts` füllt allein eine leere Tabelle – der Kontenrahmen eines
  bestehenden Bestands bleibt beim Öffnen, wie er ist (ein Standardkonto lässt sich nach Entfernen des Hakens löschen und käme
  sonst bei jedem Start wieder). Was dem Bestand gegenüber den Seed-Dateien fehlt (nur Standardkonten; weder ID noch
  Rahmen + Nummer vergeben), nennt `Database.missing_standard_accounts()` (rein lesend, Hinweis auf der Seite des
  Kontenrahmens); angelegt wird es allein über `POST /masterdata/skr/add_standard` (`add_missing_standard_accounts`: eine
  Transaktion, `INSERT OR IGNORE`, kein UPDATE, keine `overrides`, keine privaten Konten ohne Standard-Haken). `?added=` wird nur
  als Zahl gelesen (0 bis `ADDED_MAX`), die Meldung ist fester Text. Seit 2.8.2 führt er 4184 (*Kleinunternehmer-Erlöse*). Seit 2.8.0 führt der Standard-Kontenrahmen zusätzlich 1400 „Abziehbare Vorsteuer“, 4100 „Steuerfreie
  Umsätze § 4 Nr. 8 ff. UStG“, 4180 „Erlöse, die mit den Durchschnittssätzen des § 24 UStG versteuert werden“, 5170 und 5505 (Einkauf mit 5,5% Vorsteuer) – keine Auswertung hat eine
  Sonderregel für diese Nummern (`tests/test_standard_accounts_purchase.py`: alle Auswertungen und der DATEV-Stapel sind mit und ohne sie gleich). Konten, deren Bezeichnung einen
  jährlich wechselnden Durchschnittssatz nennt (5171, 5540), gehören nicht hinein; 1409 nie (die Nummer der rechnerischen EÜR-Zeile, siehe *EÜR-Ansicht*)

## Buchungssätze

- Bankbewegung = Zeile `BookingType='bank'`; ihre Buchungssätze sind Kinder
  (`entry`, `ParentBooking_ID`) und tragen selbst kein `Account_ID`
- **Die Form eines Satzes steht als Daten am Satz** (`Bookings.EntryForm`, seit 2.1.0;
  `db/matching.py`, Bankzeilen haben keine). Sechs Werte, je mit der Seite, auf die sich der
  Betrag bezieht: `purpose_first` (Zweckkonto vorne, Gegenkonto = Geldkonto oder leer; Betrag =
  Richtung auf dem Gegenkonto), `liquid_first` (Geldkonto vorne, v. a. WISO-Import; Richtung auf
  dem Konto), `transfer` (Umbuchung, beide Seiten kontiert, keine liquide; positiv = `COA` im
  Soll), `mirror_coa` / `mirror_counter` (Spiegel, beide Seiten Geldkonten; Betrag auf dem Konto
  bzw. dem Gegenkonto) und `mirror_legacy` (Spiegel des Bestands, Lesart unbekannt – gelesen wie
  vor 2.1.0: Gegenkonto-Seite, im Dashboard – zwischen eigenen Konten – als Übertrag mit dem Rohbetrag (eigenständig)
  bzw. ohne Lesung seines Betrags (Kind), in der Split-Summe über `is_bank_effective`).
  **Jeder Schreibweg setzt die Form** beim Schreiben, mit den Zahlungskonten von damals
  (`derive_entry_form`, `import_entry_form`; die App-Schreiber nennen sie ausdrücklich:
  Forderung und AfA `transfer`, Zahlungssätze `liquid_first`/`transfer`/`purpose_first`) und
  **leitet sie neu ab, wenn sich Konto oder Gegenkonto ändern** (`reform`: eine unveränderte Seite
  behält ihre Rolle, kippt die Seite des Betrags, wird er negiert – Soll/Haben bleibt; **mit dem Betrag
  dreht der Steuerbetrag**, sofern der Aufrufer ihn nicht ändert: `_form_after_accounts_change`, die eine
  Stelle für `update_booking_fields`, `update_booking`, `backfill_booking_fields` und den
  Tabellen-Export; `tests/test_entry_form_tax_sign.py`). Die Leser
  (EÜR, DATEV-Export, Dashboard, Split-Summe, Maske) **lesen die Form**; nur eine Zeile ohne Form
  (`EntryForm IS NULL`, per Roh-SQL/SQL-Konsole angelegt) wird mit den Zahlungskonten von jetzt
  eingeordnet (`read_entry_form`, `classify_entry`). Eine Änderung der Kontenliste ändert gebuchte
  Sätze also nicht mehr. Ausnahme, bewusst: Die Split-Summe zählt einen Satz ohne Spiegel-Form
  weiter über `is_bank_effective` (eigene Bankkonten) – die Form kennt die Zahlungskonten
  einschließlich 1460, nicht die eigenen Konten, und eine reine Formlesung hätte Bestand anders
  summiert
- Migration auf 2.1.0 (`_fill_entry_forms`): Der Bestand bekommt die Form, die 2.0.x ihm
  gab (Zahlungskonten von damals einschließlich 1000–1099, `_get_legacy_payment_coa_ids`; je
  Kontopaar einmal eingeordnet, gebündelt über die IDs geschrieben),
  Spiegel `mirror_legacy`; danach ist jede Auswertung identisch. **Vor jedem Migrationslauf einer
  bestehenden Datenbank** (`from_version < SCHEMA_VERSION`, Tabellen vorhanden) legt
  `initialize_database` eine Sicherung `backup/JJJJMMTT_vor_<APP_VERSION>_<Datei>` über die
  SQLite-Backup-API an: erst unter `<Name>.part`, nach der Prüfung per `os.replace` umbenannt, ein
  vorhandener Name wird nie überschrieben. Scheitert sie, öffnet die Datenbank nicht
  (`MigrationBackupError`), sie bleibt auf dem alten Stand. Die Umstellung selbst läuft in EINER
  Transaktion (`BEGIN` vor `ALTER TABLE`): Nach einem Abbruch ist die Datenbank so wie vorher. Es
  gibt kein Zurück auf 2.0.x außer über die Sicherung (Anleitung: README). Ein Spiegel des Bestands
  wird nie still umgedeutet: **Kein Import und keine Maske stellt `mirror_legacy` um** – die Importe
  erkennen ihn wieder und zählen ihn (`legacy_mirrors`), die Umstellung wäre eine Prüfliste, die den
  Nutzer entscheiden lässt (`docs/aufgaben.md`)
- Vorzeichen: Bei Zahlungen trägt der Betrag die Richtung der Geldbewegung
  (Einnahme +), egal auf welcher Seite das Zweckkonto steht. Bei Umbuchungen heißt
  positiv: `COA` im Soll, Gegenkonto im Haben. Fehlt die liquide Seite, bleibt es
  eine Zahlung (wie `is_bank_effective`). Spiegel: siehe Form

## Zahlungskonten

- Zahlungskonten (`get_payment_coa_ids`): die SKR-Konten der eigenen Bank- und Kassenkonten –
  die Kasse der App (SKR04 1600) zählt wie ein Bankkonto über ihr eigenes Konto, nicht über
  einen Bereich – plus das Verrechnungskonto 1460 (`DEFAULT_LIQUID_ACCOUNTS`; Geldtransit: WISO
  bucht darüber aufgeteilte Zahlungen und Anlagen-Umbuchungen). Das ist die Menge, an der die
  Importe das Vorzeichen ausrichten und die Maske und `insert_booking` die Form **beim Schreiben**
  ableiten – und mit der eine Zeile ohne Form gelesen wird. **1000–1099 gehört nicht mehr dazu**
  (SKR04: Vorräte; früher der SKR03-Kassenbereich): Beide WISO-Importe melden Buchungen darauf
  (`stock_range_accounts`, Hinweis auf *Daten*: Buchungen gegen diese Konten werden als
  Umbuchung gelesen und erscheinen nicht in der EÜR), und `negative_gross` zählt jeden Satz mit negativem
  Brutto und genau einer liquiden Seite (dort bestimmt die Geldbewegung das Vorzeichen – **festgeschrieben** nach der
  Messung an echten Daten: WISO schreibt den Brutto nie negativ, Konto = Soll, Umkehrungen stehen als vertauschte Konten;
  der Zähler ist der Wächter für Dateien außerhalb dieses Musters).
  Ob ein Satz in die EÜR kommt, entscheidet die
  Sektion in `get_euer_data` – Kind einer Bank- oder Kassenbewegung; eigenständig auf einer
  Kasse oder (ohne Kontofilter) auf 1460; Umbuchung zwischen Anlage- und Erfolgskonto –,
  nicht die Form und nicht die Kontenmenge; jede Buchung zählt dabei höchstens einmal
  (Schlüssel: ihre ID). Verknüpfung und Split-Summe rechnen mit den eigenen Bankkonten
  (`get_bank_coa_ids`); ein Spiegel mit bekannter Lesart wirkt in der Split-Summe nach seiner
  Position auf dem Konto der Bankbewegung (`bank_effect`); im Dashboard ist ein Spiegel zwischen eigenen
  Konten ein Übertrag (eigenständig mit dem Betrag aus Sicht der Kasse)

## Automatischer Abgleich, Kasse, AfA

- **Automatischer Abgleich** (`link_bank_to_entries`, läuft nach jedem Import; seit 2.3.14 mit Richtungsregel): Angefasst
  werden nur Bankbewegungen ohne Kinder und Sätze ohne `ParentBooking_ID` – eine bestehende Verknüpfung und der Text der
  Bankbewegung ändern sich nie. In den Stufen 1–6 (Reihenfolge im Lauf: 0, 1, 2, 4, 3, 3b, 3c, 3d, 5, 6, 6b, Nachlauf, 7) wird
  ein Satz nur verknüpft, wenn seine **Wirkung auf das Konto der Bankbewegung mit Vorzeichen** dem Betrag entspricht
  (`matching.link_effect` ∈ {`None`, `bank_effect`}; Toleranz unter 50 Minor Units = ein halber Cent), bei Gruppen die
  Summe der Wirkungen – jede automatische Verknüpfung ergibt damit eine ausgeglichene Split-Summe. `None` (nie Kandidat):
  `mirror_legacy` (Lesart unbekannt, wird nie gedeutet), ein Spiegel, der das Konto der Bewegung nicht berührt, jeder Satz
  mit einem ANDEREN Zahlungskonto auf einer Seite (Kasse, zweites eigenes Bankkonto, 1460), jeder Satz mit dem
  `Account_ID` eines anderen Kontos, ein Satz, dessen Betrag nicht als Zahl gespeichert ist – und **jeder Satz, bei dem das
  Konto der Bewegung nicht auf der Geldseite seiner Form steht** (`_MONEY_SIDES`): eine Umbuchung (`transfer`), die das
  Konto berührt oder nicht ganz kontiert ist, `purpose_first` mit dem Konto vorn, `liquid_first` mit dem Konto hinten. Dort
  wäre die Wirkung das Gegenteil des gespeicherten Betrags, `bank_effect` zählt ihn aber wie gespeichert – der Satz hinge
  an der Bewegung in der falschen Richtung, und die Split-Summe meldete nichts. Umbuchungen ohne Geldseite (Wirkung 0: „Wartekonto an Erlöskonto“, Privatanteil, AfA)
  zählen bei der Eindeutigkeit der Einzelstufen mit, werden aber nie allein verknüpft – nur in einer Gruppe mit dem Satz,
  der das Geld bewegt. Die Kandidaten sind `_Cand`-Tupel, Zugriff nur über Namen (`_do_link(bank_id, cand)` – so landet nie
  wieder ein falsches Feld im Banktext). Stufe 7 (die Forderung auf `resolved`) hat ihre eigene Auswahl und rechnet robust: *Rechnungszahlung*. Stufe 0, 6b, der Nachlauf und Stufe 7 rechneten schon mit Vorzeichen und sind
  darin unverändert. In Stufe 1 nimmt der Rückgriff „erster passender Satz des Empfängers“ nur, wenn keine Kennung trifft; zeigt
  die Kennung auf eine Umbuchung, bleibt ihr Beleg der Gruppenstufe überlassen. Bewusst nicht mehr automatisch verknüpft
  (die Richtung wäre nur vermutet): eine Umbuchung, deren Konto erst später Bankkonto wurde oder deren Gegenkonto
  leer/unbekannt ist (WISO-Textimport mit unbekanntem Konto), Sätze gegen 1460 ohne Quellklammer, Sätze gegen eine
  Kontonummer, die kein eigenes Konto ist (Bankkonto ohne SKR-Nummer). Sie bleiben frei – die Bankbewegung zeigt sich
  als „ohne Buchungssatz“, der Satz fehlt in der EÜR, bis er im Editor von Hand zugeordnet ist. Diese Regel nicht
  aufweichen – lieber eine Verknüpfung weniger als eine falsche (`tests/test_matching_direction.py`)
- Kasse = SKR04-Konto 1600 als eigenes Konto: Eine neu angelegte Kasse
  (`ensure_kasse_exists`, `insert_account`) bekommt 1600, das Konto wird bei Bedarf
  nachgelegt (`ensure_cash_coa`). Vorhandene Kassen behalten ihre Nummer (leer oder 1460);
  1460 ist das Verrechnungskonto, keine Kasse. Hat die Kasse keine SKR-Nummer, ist 1600
  kein Zahlungskonto
- AfA (`book_depreciation`): Umbuchung „Aufwand an Anlagekonto“ (direkte Abschreibung, Form
  `transfer`), Betrag positiv, ohne `Account_ID`, je Anlage und Jahr nur einmal (Sperre in einer
  Transaktion). Aufwandskonto = Erfolgskonto (4000–7999), Anlagekonto = Konto 1–999, nach
  dem Abgang der Anlage keine AfA mehr. „Gebucht“ heißt: die Buchung gibt es (oder
  WISO-`booked`). Löschen der Buchung setzt das Jahr auf `planned` zurück, `delete_asset`
  lehnt bei gebuchter AfA ab. In der EÜR (Sektion 3, ohne Steuer), nicht im Dashboard – die
  AfA ist kein Geldfluss. **Plan (`Database.depreciation_plan`, seit 2.2.6):** Die gebuchten Jahre sind Geschichte, der Rest
  verteilt sich neu. Gebucht ist, was in den Büchern steht – bei einem von der App gebuchten Jahr die Wirkung der verknüpften
  Buchung auf das Anlagekonto (`matching.depreciation_effect`: Anlagekonto im Haben +, im Soll −, nie unter 0), bei einem
  importierten Jahr (`booked` ohne Buchung) `DepreciationAmount`. **Unverändert-Regel:** Stimmt jedes gebuchte Jahr auf den
  Cent mit der Formel (`calculate_depreciation_plan`, unverändert: linear, degressiv, GWG) überein, ist der Plan genau die
  Formel – für eine nur in der App geführte Anlage ändert sich nichts. **Fortschreibung** bei abweichender Geschichte
  (Import, geänderter Anschaffungswert oder geänderte AfA-Buchung): Restbuchwert = Anschaffungswert − gebuchte Beträge, ab
  dem Jahr nach dem größten gebuchten Jahr linear nach Monaten bis zum Planende (Kaufmonat + 12 × Nutzungsdauer), ganzzahlig
  in Cent, das letzte Jahr den Rest; abgelaufene Nutzungsdauer → ganzer Rest im nächsten Jahr; Jahre mit 0 Cent entfallen;
  keine GWG-/degressive Regel; Buchwerte fortlaufend aus dem heutigen Anschaffungswert. In der Fortschreibung ist **nur das
  erste offene Jahr buchbar** (`bookable`; Lücken bekommen keine Zeile, ihr Betrag steckt im Rest). `book_depreciation` liest
  den Plan in seiner Transaktion (`BEGIN IMMEDIATE`), bucht genau den Planbetrag; Anlagenseiten, `get_book_value_at_date`
  und die Prüfungen des WISO-Imports nehmen denselben Plan. **Der Plan endet mit dem Abgangsjahr** (`SaleDate`, die ersten
  vier Zeichen): offene Jahre danach entfallen – in der Unverändert-Regel wie in der Fortschreibung –, gebuchte bleiben;
  `book_depreciation` lehnt ein Jahr danach noch vor der Planprüfung mit dem Abgang als Begründung ab, und
  `get_book_value_at_date` ist 0, sobald der Abgang erreicht ist: bei einem ISO-`SaleDate` am Abgangstag und danach, bei
  einem Datum, das kein ISO-Datum ist, aber mit einem Jahr beginnt, ab dem Jahr danach (wie das Planende); ohne lesbares
  Jahr gilt kein Abgang (die zeitanteilige AfA im Abgangsjahr bleibt offen).
  **Erweiterungen (seit 2.2.7):** Nachträgliche Anschaffungskosten (Unter-Anlagen, `Assets.Parent_ID`) erhöhen den
  Anschaffungswert der Hauptanlage. Eine Unter-Anlage **zählt** (`Database.counts_toward_parent`), wenn ihre Hauptanlage
  einen Plan haben kann (lesbares Kaufdatum, Preis > 0 Cent, Nutzungsdauer > 0), sie **gar keine eigene Zeile in
  `AssetDepreciations`** (gleich welchen Status) und keine eigenen Unter-Anlagen hat und ihr Datum und ihr Preis (> 0)
  lesbar sind. „Keine Zeile“ statt „kein gebuchtes Jahr“, weil die Planzeile nach dem Löschen der einzigen AfA-Buchung als
  `planned` stehen bleibt: Die Unter-Anlage bleibt eine eigene Anlage und lässt sich neu buchen, statt ihren Betrag in die
  Zukunft der Hauptanlage zu verschieben. Sie hat **keinen eigenen Plan** (`depreciation_plan` leer, `get_book_value_at_date`
  0), wird nicht gebucht (`book_depreciation` lehnt ab: Die AfA läuft über die Hauptanlage), hat keinen eigenen Abgang
  (`sell_asset` lehnt ab, die Seite zeigt das Formular nicht) und keinen eigenen Status (`handle_update_asset` lehnt einen
  Status ≠ `active` ab: „Eine Erweiterung behält den Status der Hauptanlage.“); Nutzungsdauer, Methode und Anlagekonto der
  Hauptanlage gelten (kein eigener Codepfad: gebucht wird allein an der Hauptanlage). Eine Unter-Anlage mit eigener AfA-Zeile
  (Bestand, WISO) oder unter einer Hauptanlage ohne Plan bleibt eine eigene Anlage mit ihrem Plan (wie bis 2.2.6) und fließt
  nicht in die Hauptanlage. **Löschen** (`delete_asset`): Eine zählende Erweiterung lässt sich nicht löschen, sobald die
  Hauptanlage seit dem Jahr der Erweiterung (nie vor ihrem Kaufjahr) ein gebuchtes Jahr (`_DEPRECIATION_BOOKED`) hat – sie
  steckt in diesen Buchungen; ohne ein solches Jahr schon. Maßgeblich ist das Anschaffungsdatum der Erweiterung (wann die Kosten
  entstehen, nicht die Zahlung), ihr Jahr nie vor dem Kaufjahr der Hauptanlage. Im Jahr der Erweiterung zählt sie **wie zu
  Jahresbeginn aufgewendet** (keine Kürzung nach Monaten): Restbuchwert plus Erweiterung verteilen sich linear nach Monaten
  auf die Restnutzungsdauer (`_continue`). **Ausnahme Anschaffungsjahr:** Die Formel rechnet auf Anschaffungswert plus
  Erweiterung, ab dem Kaufmonat der Hauptanlage. Ab der ersten späteren Erweiterung rechnet der erwartete Plan linear über die
  Restnutzungsdauer (auch bei degressiver Methode oder GWG der Hauptanlage – dieselbe Vereinfachung wie die Fortschreibung),
  und die Unverändert-Regel vergleicht die gebuchten Jahre mit diesem Plan. Entsteht eine Erweiterung in einem schon
  gebuchten Jahr, stimmt die Geschichte nicht mehr: Fortschreibung, ihr Betrag geht in den Rest ab dem ersten offenen Jahr
  (die Buchwerte der gebuchten Jahre rechnen vom Anschaffungswert samt Erweiterungen bis zu diesem Jahr). Nach dem Ende der
  Nutzungsdauer steht der ganze Betrag im Jahr der Erweiterung. `Database.acquisition_cost(asset_id, year)` = Preis plus
  zählende Erweiterungen mit Jahr ≤ `year` (Standard: laufendes Jahr; die Anlagenliste zeigt ihn). `handle_add_asset` legt
  keine Erweiterung einer Erweiterung an

## Bankkonten, Besteuerungsart, Masken, Bank-Editor

- **Konto im Kontenrahmen eines Bankkontos (seit 2.8.1, `Accounts.SKRAccount`):** Das Formular Stammdaten → Bankkonten bietet eine **Auswahl**
  (`pages_masterdata._skr_select`): die Konten des Kontenrahmens im Bereich der Art – Bank 1700–1899, Kasse 1600–1699
  (`handlers.BANK_SKR_RANGE`/`CASH_SKR_RANGE`) – und „-- keines --“. **Ein neues Bankkonto bekommt die kleinste freie Nummer** (1800, dann 1810 …;
  `handlers.free_bank_skr_number` – EINE Stelle für das Formular und für das Bankkonto der Ersteinrichtung, `handle_setup_save`, das bis 2.8.0 fest
  1200 trug: die Banknummer eines anderen Kontenrahmens, mit der sich keine Zahlung zuordnen ließ). Ein gespeicherter Wert, den die Auswahl nicht
  kennt (1200, eine alte Kasse mit 1460), steht als eigene gewählte Option da, nichts wird still umgestellt; die Liste nennt in Warnfarbe „kein Konto
  im Kontenrahmen“ bzw. „1200 nicht im Kontenrahmen“. **Speichern** (`handlers._skr_account_refusal`, EINE Prüfung für Anlegen und Ändern): Ein neuer
  oder geänderter Wert muss im Kontenrahmen stehen (die Kasse auf 1600 legt ihr Konto selbst an), im Bereich liegen und von keinem anderen eigenen
  Konto getragen werden – sonst `SKR_NOT_IN_CHART`/`SKR_OUT_OF_RANGE`/`SKR_IN_USE` auf dem Fehlerweg der Seite, nichts gespeichert; ein unverändert
  zurückgeschickter Wert ist nie ein Verstoß. Die Sperre „SKR-Konto nicht geändert: n Buchungssätze …“ kommt danach, unverändert (1200 → 1800 gelingt,
  solange kein Satz 1800 führt; nichts wird nachgetragen). Die Datenbankschicht prüft nichts (Importe, Seed, Tests). Die Meldungen von `_bank_coa_of`
  sagen, was zu tun ist („… bitte unter Stammdaten → Bankkonten … wählen (üblich: 1800)“). Prüfen und Schreiben laufen unter EINER
  Sperre (`handlers._BANK_ACCOUNT_LOCK`: Anlegen, Ändern, Ersteinrichtung – zwei gleichzeitige Anfragen bekommen nie dieselbe Nummer), und
  eine als Text gespeicherte Nummer gilt als vergeben (`_carries_skr_number`). Texte kurz halten (Entscheidung des Owners: ein Satz
  unter dem Feld, keine Erklärstücke). `tests/test_bank_account_chart_choice.py`
- **Besteuerungsart (seit 2.4.0):** Kleine Variante – die Angabe wird nur gespeichert und angezeigt. `Contacts.VatMethod` (Schema 2004)
  nennt, ob die Umsatzsteuer nach vereinnahmten (`'ist'`, Ist-Versteuerung: mit der Zahlung) oder vereinbarten Entgelten (`'soll'`,
  Soll-Versteuerung: mit der Leistung) entsteht; NULL = nicht festgelegt. Es trägt sie nur ein eigener Kontakt (`ContactType='own'`, Firma
  ODER Person – deshalb an `Contacts`, nicht an `CompanyDetails`); jeder andere bekommt NULL, und ein eigener Kontakt, der den Typ wechselt,
  verliert sie. **Die Werte stehen an EINER Stelle** (`db/contacts.py`: `VAT_METHODS`, `VAT_METHOD_LABELS`, `VAT_METHOD_ERROR`) und werden
  **streng gelesen** (`vat_method()`: genau `'ist'` oder `'soll'` als Text; alles andere – per SQL-Konsole `'IST'`, `' ist'`, `1` – gilt als
  nicht festgelegt und erscheint nie roh). `Database.get_vat_method()` liefert die Angabe des ersten eigenen Kontakts (nach ID) mit
  gültigem Wert, rein lesend. Geschrieben wird über `insert_contact`/`update_contact(vat_method=…)`: `None` lässt den gespeicherten Wert,
  `''` leert, `'ist'`/`'soll'` setzen, alles andere ist `ValueError(VAT_METHOD_ERROR)`; die Handler lesen das Formularfeld `vat_method`
  (fehlt es: unverändert; ein ungültiger Wert: 303 mit dem Klartext auf dem Fehlerweg der Kontaktseite, nichts gespeichert). **Sie ändert
  nichts an Buchungen, EÜR, Dashboard und Export:** Die App bucht Erlös und Umsatzsteuer weiter mit dem Zahlungseingang (Wartekonto →
  Erlöskonto, siehe *Rechnungszahlung*) – das Muster der Ist-Versteuerung, auch bei eingetragener Soll-Versteuerung
  (`tests/test_vat_method_setting.py` hält EÜR, Dashboard und DATEV-Stapel mit `ist`, `soll` und ohne Angabe gleich). Angezeigt wird sie
  im Formular eines eigenen Kontakts (Auswahl „Besteuerungsart (Umsatzsteuer)“ mit festem Hinweistext, `server/pages_contacts.py`; ein neu
  angelegter Kontakt bekommt das Feld wie die DATEV-Nummern erst, wenn er als „Eigen“ gespeichert ist) und – nur bei `soll` – als fester
  Hinweis unter dem DATEV-Formular auf *Daten*. Keine Steuerberatung in den Texten. Die Umsetzung der Soll-Versteuerung
  (Umsatzsteuer mit der Leistung) ist offen (`docs/aufgaben.md`) – die Einstellung nicht für Rechenlogik verwenden, bevor das entschieden ist.
- Masken schreiben nur, was sie führen (`update_booking_fields`) – Gegenkonto,
  Kategorie und fremde Kontakte dürfen beim Speichern nicht verloren gehen. Der Block „Belege“ steht außerhalb des
  Formulars, das Feld Beleg-Nr. füllt das Skript nur, wenn es leer ist (*Belege verknüpfen*). **Öffnen und unverändert
  speichern ändert nichts** (`tests/test_entry_form_roundtrip.py` prüft es für jede Bankbewegung, jeden
  Satz und das Zuordnen an einem migrierten und einem frischen Bestand, nach geänderter Kontenliste):
  Welches Konto der Editor als Zweckkonto zeigt, sagt `matching.purpose_in_counter` – `liquid_first`:
  das Gegenkonto, `purpose_first`/`transfer`: das Konto, Spiegel und Zeilen ohne Form wie in 2.0.x
  („Bank an 1460“ zeigt 1460: Konto ist ein eigenes Bankkonto, Gegenkonto nicht). Editor, Maske und
  „vorhandene Buchung zuordnen“ nutzen dieselbe Regel; ein unverändert zurückgeschicktes Konto ändert
  nichts (verglichen mit dem gespeicherten Stand, nicht mit der Kontenliste von heute), ein unverändert
  zurückgeschickter Betrag gilt beim Wechsel des Zweckkontos als „nicht geändert“ (Soll/Haben bleibt: die
  Form kippt, der Betrag wird negiert). Ausgeblendet wird ein Spiegel nur, wenn beide Seiten eigene
  Bank-/Kassenkonten sind (`is_mirror_row`) – einer mit 1460 bleibt sichtbar und bearbeitbar, die
  Summenzeile des Editors rechnet ihn wie die Prüfung (`data-sign`, `bank_effect`). **Kein Schreibweg der
  Masken schreibt dasselbe Konto auf beide Seiten** (400 mit Klartext, `_refuse_same_account`); ein so
  vorgefundener Satz wird nicht angefasst. **„Vorhandene Buchung zuordnen“ nimmt die Buchung, wie sie
  ist: Steuersatz und -betrag bleiben, solange die Zeile sie nicht ändert** – die Option der Auswahl trägt
  sie (`data-rate`, `data-tax`), das Skript füllt damit die Zeile, und der Editor schreibt die Steuer
  eines bestehenden Satzes nur, wenn das Formular sie mitbringt (`_plan_split_rows`: fehlt das Feld, oder
  sind beim Zuordnen beide leer – so schickt sie ein Browser mit altem Skript –, bleibt der gespeicherte
  Wert; bei einem Satz unter der Bankbewegung löschen leere Felder die Steuer wie in jeder Zeile). Der
  Server rechnet die Steuer einer Zeile nie nach. **Das Zuordnen überschreibt die Geldseite nicht:** Steht
  hinten (Zweckkonto vorne) schon ein anderes Zahlungskonto – Kasse, andere eigene Bank, 1460
  (`get_payment_coa_ids`) –, behält der Satz Konten und Form und zählt in der Split-Summe, wie `bank_effect`
  ihn zählt; nur ein leeres Gegenkonto füllt das Konto der Bank (`tests/test_split_editor_money_side.py`,
  Eigenschaft „Zuordnen + ✕ stellt jeden gelösten Satz wieder her“ in `test_entry_form_roundtrip.py`).
  **Datum, Steuerdatum und Empfänger eines Satzes folgen der Bankbewegung nur, wenn er sie vorher mit ihr
  geteilt hat** (gleicher Wert wie die Bankbewegung bis
  jetzt, leer = leer; je Feld für sich): Ein Satz mit eigenem Wert – eine zugeordnete Bestandsbuchung, ein
  verknüpfter Beleg – behält ihn über jedes Speichern; neue Zeilen bekommen die Werte der Bankbewegung
  (`tests/test_split_editor_assignment.py`). **Entfernt der Nutzer die Zeile eines Satzes, der nachweislich
  nicht im Editor entstanden ist – importiert (`SourceGroup`), eine AfA-Buchung (ein Plan-Jahr verweist
  auf sie) oder eine als Kind angehängte Forderung (`receivable_condition`) –, wird er abgehängt statt gelöscht** (`Database.remove_split_row`: `ParentBooking_ID` leer, ein
  Status `resolved` zurückgesetzt, sonst jede Spalte unverändert; das AfA-Jahr bleibt gebucht, der nächste
  Abgleich findet ihn wieder); jede andere Zeile wird gelöscht, ein markierter Zahlungssatz mit seiner
  ganzen Zuordnung (nicht, wenn danach mehr erstattet als gezahlt wäre: Ablehnung vor dem ersten Schreiben, siehe
  *Stornobeleg*), und `delete_transaction` aus der Liste bleibt ein ausdrückliches Löschen
  (`tests/test_split_editor_removal.py`)
- **Speichern im Bank-Editor (seit 2.3.15) ist EINE Transaktion** (`Database.save_bank_with_rows(bank_id, bank_fields, plan)`,
  `db/bookings.py`): `BEGIN IMMEDIATE` → Stand der Bankzeile lesen → **planen** (`handlers._plan_split_rows`, liest über eigene
  Verbindungen – unter der Sperre ist der Stand stabil; deshalb VOR dem ersten Schreiben, und im Plan nie etwas schreiben) →
  Bankzeile → Sätze (`_write_split_rows_in`: neu, geändert, zugeordnet; entfernte Zeilen über `_remove_split_row_in` abhängen
  oder löschen) → Prüfung der zugeordneten Zahlung → Commit; das Replay-Log (`tx.log`) wird erst danach geschrieben (Bankzeile,
  Zeilen, Abhängen). Jeder Fehler und jede Ablehnung nimmt alles zurück. **Innerhalb der Transaktion nie eine öffentliche
  Methode aufrufen, die selbst schreibt oder `BEGIN` ausführt** (`delete_transaction`, `remove_split_row`,
  `unassign_invoice_payment`, `update_booking_fields` ohne `tx`) – sie hinge an der eigenen Sperre; dafür gibt es die Bausteine
  mit `tx`/`conn` (`update_booking_fields(tx=…)`, `insert_booking(tx=…)`, `_delete_transaction_in`, `_remove_split_row_in`,
  `_split_rows_dropped`). Außerhalb bleiben, wie bisher: `consume_number`, `link_booking_to_invoice_capped` und – beim Speichern
  einer Bankzeile ohne Editor – Spiegel und automatisches Kind. **Zuordnen lässt sich nur, was die Auswahl anbietet**
  (`Database.adoptable_entry_ids`: dieselbe Bedingung wie `get_unlinked_entry_bookings`, ohne `LIMIT` – freier `entry` ohne
  `Account_ID`, nicht `resolved`, kein markierter Zahlungssatz, keine Forderung) plus die eigenen Kinder der Bewegung: Jede andere ID in `split_id` – Kind
  einer anderen Bewegung, Bankzeile, die Bewegung selbst, Forderung, unbekannt, keine Ganzzahl, doppelt – ist 400 mit „Zeile n:
  …“, auch bei einer neuen Bewegung, und nichts ist gespeichert (`tests/test_split_editor_adopt_guard.py`,
  `test_split_editor_atomic.py`). `transaction_id` wird streng gelesen (leer/0 = neu, sonst positive Ganzzahl oder 400); der
  Editor nimmt höchstens `MAX_SPLIT_ROWS` = 500 Zeilen je Anfrage an (geplant und geschrieben wird unter der Schreibsperre). **Eine veraltete
  Maske löscht nichts, was sie nie gezeigt hat (seit 2.8.5):** Der Editor nennt im versteckten Feld `split_loaded` die IDs der Zeilen, die
  er beim Öffnen gezeigt hat (`handlers._loaded_split_ids`: genau ein Feld, kanonische positive Ganzzahlen mit Komma, höchstens 5000 IDs und
  100 000 Zeichen, sonst 400). Hängt beim Speichern ein Satz unter der Bewegung, der weder in einer Zeile steht noch geladen war – eine
  Zahlung aus einem zweiten Fenster, ein Satz vom Import oder Abgleich –, lehnt das Planen ab (`SPLIT_ROWS_CHANGED`, 400, nichts
  gespeichert); bis 2.8.4 galt er als „entfernt“ und ging samt Zuordnung verloren. Fehlt das Feld (ältere Aufrufer), gilt alles wie bisher.
  Ein neues Skript der Maske, das Sätze anlegt oder löst, muss die Maske neu laden (`tests/test_split_editor_stale_mask.py`)
- **Bankbewegung mit zugeordneter Zahlung (seit 2.3.15, Entscheidung des Owners: ablehnen, nichts umrechnen):** Solange einer
  Bankbewegung nach dem Speichern noch eine Zuordnung gehört (`InvoicePayments.BookingID` zeigt auf sie oder auf einen ihrer
  Buchungssätze – so ordnet ein Import zu –: Zahlung oder Erstattung), lassen
  sich **Betrag, Bankkonto und Buchungsdatum** nicht ändern (`_assigned_bank_change_blocker`, `BankChangeRefused` → 400:
  „Dieser Bankbewegung ist eine Rechnungszahlung zugeordnet (Rechnung …). Bitte zuerst die Zuordnung an der Rechnung entfernen,
  dann … ändern.“, bei Erstattungen der Wortlaut mit der Stornorechnung) – mit und ohne Editor. Verglichen wird mit dem Stand
  vom Beginn der Transaktion (Betrag in Minor Units); ein unverändert zurückgeschickter Wert ist kein Verstoß; Empfänger, Text,
  Währung und Steuerdatum bleiben frei. Geprüft wird NACH den Schreibschritten: Wer im selben Speichern alle Zeilen der Zahlung
  entfernt, darf den Betrag mit ändern (`tests/test_bank_change_assigned_payment.py`). Die Zeilen der Zahlungssätze selbst
  lassen sich im Editor weiterhin ändern (`docs/aufgaben.md`)
