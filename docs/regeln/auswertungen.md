# DATEV-Export, Dashboard, BWA, EÜR, Summen- und Saldenliste, Liquidität

## DATEV-Export

- Nachbildung des WISO-Stapels (an einem Bestand gemessen, keine Zusicherung, dass die Kanzlei ihn annimmt). Die BU-Schlüssel sind nicht
  gegen einen echten Stapel geprüft (`docs/aufgaben.md`) – nicht als richtig voraussetzen.
- Soll/Haben-Kennzeichen bezieht sich auf das Feld „Konto“ und folgt der Form (`soll_side`).
- **Merkmal „exportiert“** (`BookingExports`): `handle_datev_export` ruft `Database.mark_datev_export` – setzt wie bisher das Steuerdatum auf heute und legt
  für jeden Satz **im Stapel** (nicht die weggelassenen und nicht die außerhalb des Zeitraums) eine Zeile an, in EINER Transaktion. Nur der erste Export zählt:
  `ExportedAt` ändert ein späterer nie. Der Stapel selbst bleibt unverändert (`tests/test_datev_export_marks.py`). Das Merkmal wirkt heute so: Das Zurückholen
  einer Rechnung fragt nach (`rechnungen.md`), und ein exportierter Zahlungssatz zählt beim Umzug der Zahlungen als „hängt etwas dran“
  (`_booking_attachments_exist`, `BOOKING_REFERENCES`). Der Stempel `DateTax` bleibt; Wirkungen und Ablösung des Stempels: `docs/aufgaben.md`.
- **BU-Schlüssel als Daten am Satz** (`Bookings.DatevKey`, ganze Zahl 1–9999, Spalte 23; nur die Importe schreiben ihn): Der Export gibt
  ihn unverändert aus (auch 490/100/121 ohne Satz); alles andere (0, über 9999, Text) gilt als keiner. Er fällt (NULL), wenn sich Konto,
  Gegenkonto oder Steuersatz ändern: `update_booking_fields`/`update_booking` über EINE Stelle (`_datev_key_stale`, im selben UPDATE),
  sofern der Aufrufer `datev_key` nicht selbst nennt; ebenso der Tabellen-Export des Textimports, der ein leeres Konto füllt. Ein unverändert
  zurückgeschickter Wert lässt ihn stehen.
- **Wo keiner gespeichert ist, leitet der Export ihn ab** (`db/datev_keys.py`, rein) nach der **Art des Zweckkontos** statt der Geldrichtung
  und dem exakten Satz:
  - Erlöskonto 4000–4999 und 6884–6892 (`DISPOSAL_REVENUE_RANGE`): Umsatzsteuer; jedes andere Zweckkonto: Vorsteuer (auch bei Erstattung);
    Debitor und Spiegel: keiner; fehlt das Zweckkonto, zählt die Geldrichtung.
  - 19 / 7 / 16 / 5 % → Vorsteuer 401/402/7/6, Umsatzsteuer 101/102/5/4; **vom 2020-07-01 bis 2020-12-31 tauschen die Plätze**
    (16/5/19/7 %). Kein Satz, 0 % und jeder andere Satz → 0 (auch AfA); bei Umsatzsteuer steht der Satz in Spalte 40. Solche Zeilen zählt
    der Export (`export_to_datev(rates_without_key=…)`, `DatevStack.hint`; fester Hinweis `DATEV_RATE_NOTE` unter dem Formular).
- **Rechnungskette** (Forderung, „Wartekonto an Erlöskonto“, Gegenbuchung einer Stornorechnung; `booking_kind`): kein Schlüssel, in Spalte 40
  der Satz an jeder Umsatzsteuer-Zeile mit Satz über 0. Leistungsdatum (114) nur an der Forderung – das Ende des Leistungszeitraums der Rechnung, sonst ihr Leistungsdatum (`get_datev_invoice_data`, `service_period`); Land (119) an der Kette (leer/D/Deutschland
  = DE, ohne Rechnung DE); Spalte 115 leer (`DateTax` setzt der Handler).
- Belegfeld 1 ohne Leerzeichen, bis 36 Zeichen. Buchungstext (Spalte 13): Steuerzeichen vor dem Kürzen auf 60 Zeichen zu Leerzeichen.
- Vorlauf: Berater- und Mandantennummer der eigenen Firma (`CompanyDetails.DatevConsultantNo`/`DatevClientNo`, ASCII-Ziffern ohne führende
  Null: Berater 1001–9999999, Mandant 1–99999; `Database.get_datev_numbers`), sonst Platzhalter und Hinweis auf *Daten*. Jeder Wert im Stapel
  ist streng geprüft – ein per SQL-Konsole gesetzter anderer Wert gilt als nicht vorhanden, nie roh ausgegeben.
- Zeitraum über den Jahreswechsel wird abgelehnt (`DATEV_YEAR_REFUSED`, `handle_datev_export`).
- **Überträge zwischen eigenen Konten werden gezählt, nicht gepaart** (`db/matching.py`: `transfer_legs`, rein `settle_transfers`,
  `Database.check_transfers`, O(n log n)):
  - Beine aus dem ganzen Bestand: Bankbewegung mit Spiegel-Kind oder ohne Kind (Richtung allein nach dem Vorzeichen der **Bankzeile**,
    `mirror_legacy` nie gedeutet), eigenständige Spiegel mit bekannter Lesart, „anders kontierte“ Bankbewegungen. Nicht teil nehmen:
    Konten ohne oder mit doppelter SKR-Nummer, Zeilen mit Datum nicht exakt `JJJJ-MM-TT` oder Betrag keine ganze Zahl.
  - Gruppe = (Ausgangskonto, Eingangskonto, Betrag); Abschnitt = Elemente bis `TRANSFER_DAY_WINDOW` = 7 Tage Abstand; Umfeld = Gruppe bis
    `TRANSFER_NEIGHBOUR_DAYS` = 14 Tage vor und nach dem Abschnitt.
  - **Die Eingangs-Sätze eines Abschnitts fallen aus dem Stapel** (alle oder keiner, kein `DateTax`), wenn jede Ausgangsbewegung ihren Satz
    trägt, `a_m ≥ 1`, `b_m ≥ 1`, `a_m ≥ b_m + b_c` und im Umfeld weder ein Ausgang ohne Satz noch ein eigenständiger Spiegel liegt – sonst
    bleibt jede Zeile stehen.
  - Eine Bankbewegung ohne Satz gilt nur als Gegenseite eines Übertrags, wenn die überschüssigen Spiegel der Gegenseite im Umfeld
    (abzüglich anders kontierter Bewegungen) für ALLE satzlosen ihrer Seite reichen.
- **Hinweise** (nie mit Text oder Empfänger): (1) Bankbewegungen im Zeitraum ohne Satz, die kein Übertrag deckt; (2) Überträge über die
  Zeitraumgrenze (je Abschnitt gezählt); (3) Sätze, die einen Übertrag möglicherweise doppelt abbilden (nicht zusammengelegt, obwohl im
  Umfeld Ausgangs-Satz, anders kontierte Gegenbewegung oder eigenständiger Spiegel steht). Mit Hinweisen exportiert der Handler nur mit
  `confirm_export` = `1` und `confirmed_check` = Prüfsumme über Zeitraum und alle gemeldeten Einträge.
- Maßstab: Bei vollständigen Auszügen und höchstens 14 Tagen zwischen beiden Seiten fällt nie ein Übertrag weg und steht keiner doppelt,
  ohne genannt zu sein (`tests/test_datev_transfer_enumeration.py`, Grenzen dort und in `DB_MODEL.md`). **Diese Regeln nicht lockern –
  lieber ein Hinweis zu viel.**
- Bekannte Grenzen: mehr als 14 Tage Laufzeit; eine ferne Lücke sperrt eine ganze Serie (genannt); Stückelung; Konten mit gleicher SKR-Nummer.

## Dashboard

- `Database.get_dashboard` (`get_dashboard_totals`/`-monthly` sind Hüllen): zerlegt **jede** gezählte Bewegung ohne Rest in vier Töpfe –
  Einnahme, Ausgabe, privat, Übertrag (`reporting.split_bank_movement`/`split_cash_entry`, rein, Minor Units); `balance` ist ihre Summe.
- Gezählt: Bankbewegungen der gewählten Konten und eigenständige Sätze auf Kassenkonten (Konto der Kasse auf einer Seite, oder `Account_ID`
  = Kasse und kein AfA-Satz). 1460 (außer als Konto der Kasse) und AfA zählen nicht. EÜR, DATEV-Export und Daten ändern sich nicht.
- **Bankbewegung mit Kindern:** Wirkung eines Satzes = `bank_effect` auf das Konto der Bewegung.
  - *privat* = Summe über Kinder mit 2100–2199 auf einer Seite: bei `transfer` −Betrag (im Konto) bzw. +Betrag (im Gegenkonto), auf beiden
    Seiten 0; bei jeder anderen Form außer den Spiegeln ihr **Betrag**, nicht ihre Wirkung („2100 an 1460“ hat Wirkung 0 und bliebe sonst
    Betriebsausgabe).
  - *Übertrag* nur, wenn die Bewegung mindestens einen Spiegel zwischen eigenen Konten trägt (`is_mirror_row`): Betrag minus Summe der
    Wirkungen aller übrigen Kinder; der Betrag des Spiegels wird nie gelesen, auch nicht bei `mirror_legacy`.
  - Der Rest (Betrag − privat − Übertrag) geht nach Vorzeichen in Einnahme oder Ausgabe. Die Formel „Betrag − privat − Wirkung der übrigen
    Kinder“ ist falsch (zöge einen privaten Umbuchungssatz doppelt ab; Beispiel −50, Spiegel −40, „6805 an Bank“ −10, „2100 an 6805“ +10:
    Übertrag −40, privat −10, Ausgabe 0).
- **Bankbewegung ohne Kind:** Deckt `settle_transfers(…).covered` (ganzer Bestand; `db/matching.py` bleibt unberührt) sie, ist sie ganz
  Übertrag, sonst nach Vorzeichen Einnahme/Ausgabe; „offen“ (`unlinked_count`) bleibt sie in beiden Fällen.
  - **Ausnahme Betrag genau 0,00 € (Entscheidung des Owners):** nicht offen (`Amount IS NOT 0`; `bank_count` zählt sie); Buchungsliste zeigt
    das graue Abzeichen „0,00“ (`pages_transactions.is_zero_movement`, `ZERO_MOVEMENT_HINT`); die Liquiditätsrechnung zählt sie nicht unter
    den Bankbewegungen ohne Satz (`tests/test_zero_amount_movement.py`).
  - Die Rechnung läuft nur, wenn so eine Bewegung zu zählen ist, und einmal je `get_dashboard`.
- **Eigenständiger Satz auf der Kasse** (Betrag aus Sicht der Kasse, `_cash_amount_sql`): Spiegel zwischen eigenen Konten (`is_mirror_row`,
  jede Form; `mirror_legacy` und Zeilen ohne Form mit Rohbetrag) = Übertrag, 2100–2199 auf einer Seite = privat, sonst nach Vorzeichen.
- **Kontofilter:** Ein Übertrag auf ein nicht gewähltes eigenes Konto bleibt Übertrag (netto unter „Überträge“). Die Kassenseite eines
  Spiegel-Kindes unter einer Bankbewegung zählt nicht (stünde doppelt da).
- Bekannte Grenzen: unkontierter Rest neben einem Spiegel steckt im Übertrag; Konten ohne oder mit doppelter SKR-Nummer nehmen an der
  Deckung nicht teil; ein eigenständiger `mirror_legacy`-Spiegel auf der Kasse deckt die satzlose Bankseite nicht; die Deckung setzt
  vollständige Auszüge voraus (`docs/aufgaben.md`).
- Beträge aus der Datenbank nur über `_minor_or_zero` (Text, NULL, Kommazahl ab 2^63: 0); ein Datum, das SQLite nicht liest, steht nur in
  den Summen – nie ein Fehler.
- Seite: Zeile „Überträge zwischen eigenen Konten“ (`.dashTransfers`) nur bei Wert ≠ 0 (auf den Cent), im Monats-Tooltip nur bei ≠ 0, ohne
  Balken; Werte maskiert. Test: `tests/test_dashboard_split.py`; `entry_form_golden_2_0_8.json` gilt nur über Saldo, Anzahl, Monatssummen.

## Gemeinsames der Auswertungen (BWA, EÜR, SuSa, Liquidität)

- Seiten nehmen den gemeinsamen Zeitraum (`from`/`to`, sonst Cookie, sonst laufendes Jahr – `resolve_period`; das Cookie setzt die Route);
  ungültiger Wert wird ohne Hinweis verworfen. Monatsauswertungen leiten den Monat ab (`period.period_month`, siehe `oberflaeche.md`);
  `year`/`month` liest die Seite nicht.
- PDFs lesen ihre Parameter streng (doppelter Schlüssel ungültig, 400), setzen kein Cookie, landen nie auf der Platte (`appDownload`).
  PDFs und CSV stehen nicht in der Positivliste des Diagnose-MCP; die Seiten `/bwa`, `/euer`, `/susa`, `/liquiditaet` stehen darin
  (schreiben nichts; Nachweis im Test). Fester Hinweis: keine Steuerberatung; keine Marken- und Produktnamen.
- Untermenü: `REPORT_PAGES`, `reports_submenu` in `server/pages_bwa.py`; jede Seite ruft `Header1('bwa')` auf.
- Alle haben keine eigene Lesart der Buchungen; Zahlen kommen aus `get_euer_data` bzw. den Zeilen des DATEV-Exports.

## BWA

- `db/bwa.py`, `server/pages_bwa.py`, `export/pdf_bwa.py`, `export/csv_bwa.py`; `GET /bwa`, `/bwa/pdf`, `/bwa/csv`. Reine Umgruppierung von
  `get_euer_data` ohne Kontofilter. Einnahme/Ausgabe/„nicht im Ergebnis“ entscheidet EINE Funktion, `reporting.euer_section` (nutzt auch
  das Dashboard; `tests/test_bwa.py` hält die Regel für alle Kontonummern fest).
- Gruppen als Daten in `db/bwa.py` (`RANGES`, `EXCEPTIONS`, `group_of`); keine nutzerpflegbaren Gruppen (Schemaänderung). Ein Konto ohne
  Gruppe steht unter „Nicht zugeordnet“ und zählt im Ergebnis – nie still in einer anderen Gruppe. Konten 3800–3849 außer 3806 gehen je
  Konto und Zeitraum nach Vorzeichen des Saldos zu Erstattungen (> 0, unter Betriebseinnahmen) bzw. Zahlungen.
- Invarianten (Test, Minor-Wert, Testwelt und Demobestand): Ergebnis = EÜR-Ergebnis; Betriebseinnahmen − Erstattungen = Summe der
  EÜR-Einnahmen; Betriebsausgaben − Erstattungen = −Summe der EÜR-Ausgaben.
- Gerechnet nur in Minor Units, Prozente erst bei der Ausgabe: Anteil leer bei Bezugsgröße ≤ 0, Veränderung in % leer bei Vorjahreswert 0
  (und bei Quotient, der den Decimal-Kontext sprengt). Gerundet wird erst bei der Anzeige; eine gedruckte Summe kann bei Cent-Bruchteilen um
  wenige Cent von der Summe der Zeilen abweichen – nicht glätten, die Gleichheit mit der EÜR geht vor.
- Zwei Konten gleicher Nummer stehen im Kontennachweis als eine Zeile unter dem zuerst gefundenen Namen.
- Monat und aufgelaufen ab 1. Januar; Darstellung: Vorjahresvergleich (Standard, auch bei ungültiger Angabe) oder `variant=jahr` (Monat
  und Jahr mit Anteilen); Auswahl als GET-Formular mit `from`/`to` im Inhalt. PDF: A4 quer, `parse_year`/`parse_month`/`parse_variant`,
  `BWA_JJJJ-MM.pdf`, mit Anteilen `…_Anteile.pdf`.
- **CSV:** dieselben Zeilen und Zellen wie die Seite (`cells`), auf Wunsch mit Konten; UTF-8 mit BOM, Semikolon, Dezimalkomma ohne
  Tausenderpunkte, Prozentwerte ohne Zeichen. Texte aus der Datenbank nur über `csv_table.text_cell` (Steuerzeichen weg, höchstens 200
  Zeichen, `'` vor `=`, `+`, `-`, `@` – auch hinter jedem Komma und vor breiten Schreibweisen, verglichen in NFKC; sonst führte eine
  Tabellenkalkulation einen Kontonamen als Formel aus). **Nicht aufweichen.**
- Tests: `tests/test_bwa.py`, `tests/test_bwa_pdf.py`, `tests/test_report_csv.py`.

## Vorsteuer in der EÜR

- `get_euer_data`: Die Vorsteuer aus einer Ausgabe steht für JEDEN Satz in der EÜR. Reihenfolge:
  1. 1401 (5/7 %) bzw. 1406 (16/19 %), Vergleich `abs(Satz − x) < 0,0001` wörtlich, wenn der Kontenrahmen das Konto führt.
  2. Als Prozentzahl gespeicherter Satz über die feste Tabelle der Summen- und Saldenliste.
  3. Vorsteuerkonto des ersten Stammdaten-Eintrags mit Konto für diesen Satz (`db.tax_rates.select_report_tax_accounts`: nach Reihenfolge
     und ID; ungültige Zeilen, Satz 0, die festen vier Sätze und Konten außerhalb des Bereichs zählen nicht).
  4. Sonst die rechnerische Zeile **1409 „Vorsteuer ohne Steuerkonto“** (`reporting.EUER_INPUT_TAX_UNASSIGNED`, ab 1 Cent, BWA-Gruppe
     „Gezahlte Vorsteuer“) – auch bei Steuerbetrag ohne Satz und bei 19/7 % mit gelöschtem 1406/1401.
- Die Umsatzsteuer bleibt EINE virtuelle Zeile 3806 (das Umsatzsteuerkonto eines Eintrags wirkt nur in der Summen- und Saldenliste).
- Gelesen wird der Stand der Stammdaten von heute (verschiebt Steuer früherer Zeiträume zwischen Sammelzeile und Konto; Summen bleiben).
- Grenzen: Einordnung 6884–6892 (EÜR: Vorsteuer, Liste: Umsatzsteuer) gilt für jeden Satz; Zweckkonten, die kein Aufwand sind (Privat,
  Darlehen), führen ihre Steuer bei 19 % als Vorsteuer. Test: `tests/test_tax_accounts_reports.py`.

## EÜR-Ansicht

- `db/euer.py`, `server/pages_euer.py`, `export/pdf_euer.py`; `GET /euer`, `/euer/pdf`. EINE Aufbereitung für Dashboard-Kasten und Seite:
  `db.euer.euer_statement` (Zeilen von `get_euer_data` nach `reporting.euer_section` in Einnahmen, Ausgaben, „Sonstige Ausgaben“; Summen aus
  Minor Units, nie `float`; echte und virtuelle 3806 bleiben zwei Zeilen; Ergebnis = Einnahmen + Ausgaben, „Sonstige Ausgaben“ zählen nicht).
- Der Dashboard-Kasten bleibt **zeichengleich** – bis auf die Zeile mit dem Link „zur Auswertung“ (mit „(dort ohne Kontofilter)“, wenn Konten
  gewählt sind): SHA-256-Schnappschüsse (`tests/test_euer_view.py`); ändert sich ein Zeichen, ist der Code zu ändern, nie der Schnappschuss.
- Leerer Adresswert (`from=`) gilt als nicht angegeben. **Kein Kontofilter** auf der Seite (mit Filter fielen 1460 und AfA weg). `from` nach
  `to` wird auf der Seite getauscht (`valid_period`); Menüzeile ist `period_filter_widget`.
- Das PDF liest den Zeitraum streng (`period.parse_iso_date`): beide nötig, `from` nicht nach `to` – **kein stiller Tausch** –, 400
  „Ungültiger Zeitraum.“. A4 hoch; `EUER_JJJJ.pdf` (volles Kalenderjahr) sonst `EUER_von_bis.pdf`. Bausteine in `export/pdf_table.py`
  (gelten auch für `pdf_bwa`, dessen Ausgabe gleich blieb: `tests/test_pdf_table.py`). Hinweis: kein amtliches Formular.

## Summen- und Saldenliste

- `db/susa.py`, `Database.get_susa`, `server/pages_susa.py`, `export/pdf_susa.py`, `export/csv_susa.py`; `GET /susa`, `/susa/pdf`,
  `/susa/csv`. Je Konto Anfangssaldo, Soll/Haben im Monat und aufgelaufen seit 1. Januar, Endsaldo (`1.234,56 S`/`H`).
- **Dieselben Zeilen wie der Export für das Steuerbüro:** `accounts.LEDGER_ROWS_WHERE` = `WHERE` von `fetch_bookings_range` (alle Sätze außer
  Bankbewegungen bis Monatsende; Vorjahre bilden den Anfangssaldo); Form und Soll-Seite aus `matching.ledger_form`/`coa_in_soll` wie
  `export_to_datev` (Export Byte für Byte unverändert, SHA-256-Schnappschüsse in `tests/test_susa.py`: Code ändern, nie den Schnappschuss);
  Art aus `datev_keys.booking_kind`, Wartekonten aus `get_waiting_account_numbers`; es fehlen die Eingangs-Sätze gepaarter Überträge
  (`settle_transfers(…).omitted`) und gedeckte Bankbewegungen ohne Satz (`.covered`). Ein Test hält für JEDE Zeile der Testwelt und des
  Demobestands fest, dass die Liste dieselbe Seite bucht wie die Exportzeile.
- **Buchungsregel** (`entry_postings`, rein, Minor Units): Betrag ohne Vorzeichen; `purpose_first` und `mirror_counter` positiv Gegenkonto
  im Soll; `liquid_first`/`transfer`/`mirror_coa` positiv Konto im Soll. `mirror_legacy` bucht nie (Hinweis).
- **Steuer:** Der **gespeicherte** Steuerbetrag wird aus der Seite des Zweckkontos herausgerechnet (nichts nachgerechnet) und steht auf
  1401/1406 (Vorsteuer) bzw. 3801/3806 (Umsatzsteuer; 16 % und 5 % auf den Konten von 19 % und 7 %, `TAX_ACCOUNTS`). Anderer Satz: Konto der
  Stammdaten (`SusaContext.tax_accounts`; die feste Tabelle gilt zuerst, der Export kennt diese Konten nicht), sonst „Steuer ohne
  Steuerkonto“. Ein Steuerkonto, das der Kontenrahmen nicht führt, trägt „(nicht im Kontenrahmen)“. Forderung einer Rechnung und
  Gegenbuchung der Stornorechnung (Debitor auf einer Seite) werden nie zerlegt.
- **Kontenklassen und Vortrag an EINER Stelle** (`GROUPS`, `group_key`, `is_carried`): Bestandskonten und Wartekonten 4340/4345/4405
  (Gruppe „Offene Posten“) tragen vor; Erfolgskonten 4000–7999 beginnen bei 0, ihre Vorjahressumme steht auf dem virtuellen Konto „Ergebnis
  der Vorjahre (rechnerisch)“ beim Eigenkapital; Privat- und Steuerkonten werden vorgetragen; keine Eröffnungsbilanz.
- **Ausgleich:** Jede Buchung schreibt gleich viel ins Soll wie ins Haben; was fehlt, steht auf dem virtuellen Konto „Nicht kontiert“ (Seite
  ohne Konto oder Nummer, Gegenseite einer Bankbewegung ohne Satz, die kein Übertrag deckt). Summe Soll = Summe Haben je Spalte, Summe der
  Anfangs- und Endsalden 0.
- Satz unter einer Bankbewegung ohne Geldseite zählt für das Konto der Bankbewegung (Hinweis; ohne Nummer „Nicht kontiert“). Zeile ohne
  Form wird wie im Export mit den Zahlungskonten von heute gelesen (Hinweis).
- **Was sich nicht buchen lässt** (`mirror_legacy`, Betrag keine ganze Zahl bis 10^15 Minor Units, Datum kein Kalendertag, Zeile ohne
  Buchungsart, Satz mit Betrag 0 und Steuerbetrag) steht nur in den **Hinweisen** (`NOTICE_TEXTS`) – nie ein Fehler, nie still.
  `unbalanced` rechnet `get_susa` mit den lesbaren Beträgen selbst (`_susa_unbalanced`, Regel `bank_effect` wie `find_unbalanced_splits`).
- **Gültiges Datum** = Kalendertag als Text `JJJJ-MM-TT` ab Jahr 0001 (`_SUSA_DATE_OK`: `typeof = 'text'`, Muster und `x IS date(x)`): EINE
  Bedingung für gebuchte Sätze und Bankbewegungen ohne Satz. `bad_date`: Zeile mit Buchungsart und ungültigem Datum, auch Bankbewegung,
  **ohne Fenster**; `unknown_type`: `BookingType` NULL; `not_readable`: gültiges Datum bis Monatsende, Betrag unlesbar. Jede Zeile in genau
  einem Hinweis (Reihenfolge Buchungsart, Datum, Betrag). Andere Buchungsart als `entry`/`bank` liest der Export wie einen Satz, so bucht sie
  die Liste. `zero_amount_tax`: Satz mit Betrag 0 und Steuerbetrag bucht nichts.
- **Abstimmungsproben** (Pflicht-Tests, Minor-Wert): (a) Summe Soll = Summe Haben, Anfangs- + Endsalden 0, jeder Bestand und Monat, auch
  Zufallsbestände (fester Seed); (b) eigenes Bankkonto (keine Kasse, eigene SKR-Nummer): Endsaldo = Summe aller Bankbewegungen bis
  Monatsende (nach Datum als Text; „Abgleich mit den Bankbewegungen“ auf der Seite; Abweichungen werden gezählt als `child_other_period`,
  `child_other_account`, ein Spiegel zwischen eigenen Konten zählt nicht); (c) je EÜR-Zeile außer 3806 Saldo der EÜR = Haben − Soll der
  Liste, Umsatzsteuer der EÜR = 3801 + 3806 (Sätze ohne Bankbewegung, Umbuchungen ohne Zahlung und `resolved` zählt die Liste nach ihrem
  Datum, die EÜR nicht).
- **Laufzeit:** Sätze werden in SQL nach dem gruppiert, was ihre Lesart bestimmt, und je Gruppe einmal gebucht (`book_group`, linear) – nie
  eine Zeile je Satz in Python. Bei Summe über 2^63 Minor Units rechnet `reporting._sum_groups` dieselbe Abfrage in Python (jeder andere
  Datenbankfehler geht weiter). Ein Name, der kein Text ist, wird als Text gelesen (`_name_text`).
- PDF: A4 quer, `parse_year`/`parse_month`, `Summen_und_Salden_JJJJ-MM.pdf`. Fester Hinweis: ohne Zusicherung der Richtigkeit. **Diese Regeln
  nicht still ändern.**
- **CSV:** je Konto und Gruppe die sechs Werte immer als Zahl, Salden als Betrag mit eigenem Kennzeichen S/H, am Ende die Gesamtsumme;
  Hinweise und Bankabgleich nur auf Seite und PDF.
- Tests: `tests/test_susa.py`, `tests/test_susa_pdf.py`, `tests/test_report_csv.py`.

## Liquiditätsrechnung

- `db/liquidity.py`, `Database.get_liquidity`, `server/pages_liquidity.py`, `export/pdf_liquidity.py`; `GET /liquiditaet`,
  `/liquiditaet/pdf`. Vom BWA-Ergebnis zur Veränderung der flüssigen Mittel; Monat und aufgelaufen; Vorjahresvergleich (Standard, acht
  Wertspalten) oder `variant=jahr` (zwei Spalten, **keine Anteile**; `pages_bwa.read_variant`: unbekannt/doppelt → Standard;
  `pages_bwa.read_pdf_query`: 400).
- **Keine eigene Lesart.** Gerechnet wird nur mit: den Zeilen von `get_euer_data` (ohne Kontofilter, `euer_section`; je Spanne ein Aufruf mit
  allen Zeilen und einer mit `only_asset_transfers=True`: nur Umbuchungen Anlage ↔ Erfolgskonto ohne Geldfluss – Sektion 3 der EÜR und
  solche UNTER einer Bankbewegung (`is_asset_transfer`); Standard ohne Schalter unverändert), dem BWA-Bericht (`build_report`) über die
  vollen Zeilen und den Dashboard-Töpfen (`_dashboard_movements`, je Spanne EIN Aufruf; Einnahme + privat + Ausgabe + Übertrag =
  `get_dashboard(…)['balance']`). Einzige neue Abfrage: `_liquidity_unbooked` (Bankbewegungen ohne Satz, die kein Übertrag deckt; Auswahl
  wie der Zweig „ohne Kind“ des Dashboards, `_minor_or_zero`, `_settle_stock` nur bei ganzzahligem Betrag ≠ 0, höchstens einmal je Aufruf).
  Schnappschüsse (BWA-PDF, EÜR-Kasten, SuSa-Export) bleiben unverändert.
- **Zeilen** (Minor Units, Wirkung auf die flüssigen Mittel, Zufluss +; `R` = Saldo der EÜR-Zeile je Konto, `U` = Saldo des Kontos in den
  Umbuchungen Anlage ↔ Erfolgskonto):
  1. Ergebnis = BWA. 2. Abschreibungen = −Σ U der BWA-Gruppe `depreciation` (6200–6299). 3. Anlagenabgänge zum Restbuchwert = −Σ U der Konten
     4855–4858, 6895–6898 (`DISPOSAL_RANGES`). 3a. Übrige Umbuchungen ohne Geldfluss = −Σ U jedes anderen Kontos (nur bei Wert).
  4. Darlehen/Verbindlichkeiten im Ergebnis = BWA-Gruppe `liabilities` (3000–3799 außer 3160, 3720, 3740), herausgerechnet.
  5. Zwischensumme „Ergebnis vor Abschreibungen und Darlehen“ = 1 + 2 + 3 + 3a + 4.
  6. Anlagenzugänge = Σ R der Konten unter 1000 außerhalb des Ergebnisses. 7. Darlehen/Verbindlichkeiten = Σ R aller Konten 3000–3799.
  8. Privatentnahmen 2100–2179. 9. Privateinlagen 2180–2199. 10. Übrige Konten außerhalb des Ergebnisses (erwartet 0, nur bei Wert).
  11. Veränderung laut Überleitung = 5 bis 10. 12. Bankbewegungen ohne Buchungssatz (Bezeichnung nennt ihre Zahl).
  13. **Nicht zugeordnet** = Ist − (11 + 12), steht immer da, auch bei 0,00.
  14. „Ist“ = Einnahme + privat + Ausgabe aller Bank- und Kassenkonten **ohne Überträge zwischen eigenen Konten**; Überträge und Veränderung
      laut Kontobewegungen nur nachrichtlich und nur, wenn Überträge dastehen.
- **Die Überleitung rechnet nur zurück, was die EÜR als Umbuchung Anlage ↔ Erfolgskonto liest** (AfA, Restbuchwert beim Abgang): Eine
  EÜR-Zeile allein füllt 2, 3, 3a nie; eine direkt bezahlte Abschreibung („6260 an Bank“) bleibt im Ergebnis. Prüfreihenfolge:
  `DISPOSAL_RANGES`, dann Gruppe `depreciation`, sonst 3a (`_noncash_line`). Der Kontennachweis zeigt den Betrag der Umbuchungen, nicht den
  ganzen Saldo.
- Bereiche als Daten an EINER Stelle (`DISPOSAL_RANGES`, `FINANCING_RANGE`, `WITHDRAWAL_RANGE`, `DEPOSIT_RANGE`, `ASSET_LIMIT`), nur für
  ganzzahlige Kontonummern. **Jede EÜR-Zeile steht an genau einer Stelle** (ein Test je Grenze). Zwei Zeilen gleicher Nummer werden je
  Zeitraum addiert und stehen als EINE Zeile unter dem zuerst gefundenen Namen.
- **Abstimmung:** „Nicht zugeordnet“ ist per Definition die Differenz, keine dritte Lesart – nie einer anderen Zeile zuschlagen. „Geht auf“
  = exakt 0 Minor Units; ein Rest unter einem halben Cent steht als 0,00 da, wird als Rundungsrest genannt, nie geglättet.
- **Grenze der Ist-Seite** (die des Dashboards): Trägt eine Bankbewegung einen Spiegel zwischen eigenen Konten, steckt der von keinem Satz
  erklärte Teil im Übertrag – ein unstimmiger Split neben einem Übertrag steht nur nachrichtlich unter den Überträgen.
- **Pflicht-Tests** (`tests/test_liquidity.py`, Hilfen `tests/liquidity_helpers.py`): „Nicht zugeordnet“ ist im Demobestand und in
  Zufallsbeständen (`random_books`) in jedem Monat und aufgelaufen 0; feste Werte der SuSa-Welt und Testwelt sind der Maßstab; für jeden
  benannten Grenzfall (1460-Satz in anderem Monat, Datum über Monatsgrenze, unstimmiger Split, Steuersatz außer 5/7/16/19 %, `resolved`,
  Kasse als `Account_ID`, Spiegel alter Art, Rundungsrest) und jede aufgehende Konstellation („6260 an Bank“, „6815 an 0520“, Abgang über
  1460, Umbuchung unter einer Bankbewegung) ist festgehalten, was dort landet. **Diese Erwartungen nicht lockern** – ändert sich ein Wert,
  ist die Ursache zu klären.
- Seite: Skript nur `var`/`function` mit Präfix `liq`, vorhandene Klassen (`bwaTable` …), Zeilen mit Konten aufklappbar. Fester Hinweis:
  „Aus den Buchungen dieser App gerechnet. Keine Steuerberatung; ersetzt nicht die Auswertung eines Steuerbüros.“ PDF: A4 quer,
  `Liquiditaet_JJJJ-MM.pdf` (`…_Monat_Jahr.pdf`), auf Wunsch mit Kontennachweis, Textblock zur Abstimmung. Die Routen stehen in keiner der
  Mengen `_ACTION_PATHS`, `_JSON_PATHS`, `_LOCK_FREE_ROUTES`.
- Laufzeit: je Spanne zwei `get_euer_data`-Aufrufe; den größten Teil macht `_settle_stock` (liest den ganzen Bestand), die Liquiditätsrechnung
  ruft es einmal je Aufruf.
- Tests: `tests/test_liquidity.py`, `tests/test_liquidity_pdf.py`.
