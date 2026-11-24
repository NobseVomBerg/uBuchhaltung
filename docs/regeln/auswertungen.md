# DATEV-Export, Dashboard, BWA, EÜR, Summen- und Saldenliste, Liquidität

## DATEV-Export

- DATEV (seit 2.3.0 dem Stapel nachgebildet, den WISO schreibt – gemessen an einem Datenbestand, keine Zusicherung, dass die
  Kanzlei ihn so annimmt): Das Soll/Haben-Kennzeichen bezieht sich auf das Feld „Konto“ und folgt der Form
  (`soll_side`: steht bei positivem Betrag das Feld „Konto“ im Soll, ist es bei positivem Betrag S).
  **Der BU-Schlüssel steht als Daten am Satz, wenn die Quelle ihn mitbringt** (`Bookings.DatevKey`, ganze Zahl 1–9999, Zeilenspalte 23;
  nur die Importe schreiben ihn, kein App-Schreibweg): Der Export gibt ihn unverändert aus – auch 490/100/121 ohne Satz; alles andere in der
  Spalte (0, über 9999, Text) gilt als keiner. **Er fällt (NULL), wenn sich Konto, Gegenkonto oder Steuersatz gegenüber dem gespeicherten Stand
  ändern** – `update_booking_fields` und `update_booking` über EINE Stelle (`_datev_key_stale`, Satz als Prozentzahl, im selben UPDATE),
  sofern der Aufrufer `datev_key` nicht selbst nennt, dazu der Tabellen-Export des Textimports, der ein leeres Konto aus der Kategorie füllt
  (dieselbe Stelle); ein unverändert zurückgeschickter Wert lässt ihn stehen (auch das Zuordnen im Editor, das ein leeres Gegenkonto füllt:
  dann fällt er). **Wo keiner gespeichert ist, leitet der Export ihn ab** (`db/datev_keys.py`, reine Funktionen) – nach
  der **Art des Zweckkontos** statt der Geldrichtung (Erlöskonto 4000–4999 und 6884–6892 [„Erlöse aus Verkäufen … (bei Buchverlust)“ im
  Aufwandsbereich, `DISPOSAL_REVENUE_RANGE`]: Umsatzsteuer, jedes andere Zweckkonto: Vorsteuer – auch bei einer
  Erstattung; Debitor und Spiegel: keiner; fehlt das Zweckkonto, zählt die Geldrichtung) und dem exakten Satz: Regelsatz 19 / ermäßigt 7 /
  „anderer Regelsatz“ 16 / „anderer ermäßigter“ 5 % → Vorsteuer 401/402/7/6, Umsatzsteuer 101/102/5/4, **vom 2020-07-01 bis 2020-12-31 tauschen
  die Plätze** (16/5/19/7 %); kein Satz, 0 % und jeder andere Satz → 0 (so auch die AfA, die Umbuchung „Aufwand an Anlagekonto“: Vorsteuer-Art,
  aber ohne Steuersatz). Die **Rechnungskette** (Forderung, „Wartekonto an Erlöskonto“,
  Gegenbuchung einer Stornorechnung: Umbuchung mit Debitor oder Wartekonto, `booking_kind`) trägt keinen Schlüssel, aber in Spalte 40 den Satz
  (`19,00`, an jeder Umsatzsteuer-Zeile mit Satz über 0); Leistungsdatum (114) nur an der Forderung (das der Rechnung, sonst das Buchungsdatum),
  Land (119) an der Kette (zwei Buchstaben groß, leer/D/Deutschland = DE, sonst leer; ohne Rechnung DE), Spalte 115 leer (`DateTax` setzt der
  Handler weiter). Belegfeld 1 ohne Leerzeichen, bis 36 Zeichen; im Buchungstext (Spalte 13) werden Zeilenumbrüche, Tabulatoren und andere
  Steuerzeichen vor dem Kürzen auf 60 Zeichen zu Leerzeichen. Der Vorlauf trägt Berater- und Mandantennummer der eigenen Firma
  (`CompanyDetails.DatevConsultantNo`/`DatevClientNo`, ganze Zahlen aus ASCII-Ziffern ohne führende Null: Berater 1001–9999999, Mandant 1–99999;
  im Kontaktformular der eigenen Firma gepflegt, `Database.get_datev_numbers`), sonst Platzhalter und ein Hinweis auf *Daten*. Jeder Wert, der in den Stapel geht, ist streng geprüft –
  ein per SQL-Konsole gesetzter anderer Wert gilt als nicht vorhanden, nie roh ausgegeben. Die Schlüssel sind nicht gegen einen echten Stapel
  der Kanzlei geprüft (`docs/aufgaben.md`) – nicht als richtig voraussetzen. **Vollständigkeit (seit 2.3.4, `handle_datev_export`):** Ein
  Zeitraum über den Jahreswechsel wird abgelehnt (`DATEV_YEAR_REFUSED`: der Stapel kennt nur Tag und Monat, Wirtschaftsjahr =
  Kalenderjahr). **Überträge zwischen eigenen Konten werden gezählt, nicht gepaart** (`db/matching.py`: `transfer_legs` baut aus dem
  ganzen Bestand Beine – Bankbewegung mit Spiegel-Kind oder ganz ohne Kind, Richtung allein nach dem Vorzeichen der **Bankzeile**,
  `mirror_legacy` wird nie gedeutet –, eigenständige Spiegel mit bekannter Lesart und „anders kontierte“ Bankbewegungen (Kinder, aber
  kein Spiegel-Kind); `settle_transfers` ist die reine Rechnung, `Database.check_transfers` der Aufruf, O(n log n). Nicht teil nehmen
  Konten ohne oder mit doppelter SKR-Nummer und Zeilen, deren Datum nicht exakt `JJJJ-MM-TT` oder deren Betrag keine ganze Zahl ist).
  Gruppe = (Ausgangskonto, Eingangskonto, Betrag); Abschnitt = Elemente mit höchstens `TRANSFER_DAY_WINDOW` = 7 Tagen Abstand; Umfeld =
  Elemente der Gruppe bis `TRANSFER_NEIGHBOUR_DAYS` = 14 Tage vor und nach dem Abschnitt. **Die Eingangs-Sätze eines Abschnitts fallen
  aus dem Stapel** (alle oder keiner, kein `DateTax`), wenn jede Ausgangsbewegung ihren Satz trägt und es mindestens so viele Ausgänge
  wie Eingänge gibt (`a_m ≥ 1`, `b_m ≥ 1`, `a_m ≥ b_m + b_c`) und im Umfeld weder ein Ausgang ohne Satz noch ein eigenständiger Spiegel
  liegt – sonst bleibt jede Zeile stehen. Eine Bankbewegung ohne Satz gilt nur als Gegenseite eines Übertrags, wenn die überschüssigen
  Spiegel der Gegenseite im Umfeld – abzüglich der dort anders kontierten Bewegungen – für ALLE satzlosen ihrer Seite reichen.
  **Hinweise** (nie mit Text oder Empfänger): (1) Bankbewegungen im Zeitraum ohne Buchungssatz, die kein Übertrag deckt; (2) Überträge
  über die Zeitraumgrenze – weggelassene Eingangs-Sätze, deren Ausgangsseite außerhalb liegt, und umgekehrt Ausgangs-Sätze, deren
  weggelassene Eingangsseite außerhalb liegt (je Abschnitt gezählt: eine Serie gleich hoher Überträge löst ihn nur an einer wirklich
  überquerten Grenze aus); (3) Sätze, die einen Übertrag möglicherweise doppelt abbilden (nicht zusammengelegt, obwohl im Umfeld ein
  Ausgangs-Satz, eine anders kontierte Gegenbewegung oder ein eigenständiger Spiegel steht; mit dem ältesten Auslöser). Mit Hinweisen
  exportiert der Handler nur mit `confirm_export` = `1` und `confirmed_check` = Prüfsumme über Zeitraum und alle gemeldeten Einträge
  (ändert sich der Bestand, kommt der Hinweis neu; Muster wie beim Import der WISO-Datenbank in einen nicht leeren Bestand). Maßstab,
  per Aufzählung kleiner Bestände geprüft (`tests/test_datev_transfer_enumeration.py`, benannte Grenzen dort und in `DB_MODEL.md`): Bei
  vollständigen Auszügen und höchstens 14 Tagen zwischen den beiden Seiten fällt nie ein Übertrag weg und steht keiner doppelt, ohne
  genannt zu sein. Diese Regeln nicht lockern – lieber ein Hinweis zu viel. Bekannte Grenzen: mehr als 14 Tage Laufzeit, eine ferne
  Lücke sperrt eine ganze Serie (genannt), Stückelung (eine Bewegung gegen mehrere kleinere), Konten mit gleicher SKR-Nummer

## Dashboard und Auswertungen

- **Dashboard** (`Database.get_dashboard`, seit 2.4.1; `get_dashboard_totals`/`-monthly` sind dünne Hüllen darum): Es folgt den
  Kontobewegungen und zerlegt **jede** gezählte Bewegung ohne Rest in vier Töpfe – Einnahme, Ausgabe, privat, Übertrag
  (`reporting.split_bank_movement`/`split_cash_entry`, reine Funktionen in Minor Units); `balance` ist ihre Summe. Gezählt wird
  wie vorher (`bank_count` und `unlinked_count` ändern sich nicht, der Saldo nur bei Werten, die allein die SQL-Konsole schreibt:
  ein Kassensatz in Spiegel-Form mit einem Privatkonto, Beträge mit Bruchteil oder als Text): die Bankbewegungen der gewählten Konten und die
  eigenständigen Sätze auf Kassenkonten (Konto der Kasse auf einer Seite, oder `Account_ID` = Kasse und kein AfA-Satz); 1460
  (außer als Konto der Kasse) und die AfA zählen nicht. EÜR, DATEV-Export und gespeicherte Daten ändern sich nicht.
  **Bankbewegung mit Kindern:** Die Wirkung eines Satzes ist `bank_effect` auf das Konto der Bewegung, wie in der Split-Summe.
  *privat* = Summe über die Kinder mit 2100–2199 auf einer Seite: bei einer Umbuchung (`transfer`) −Betrag (im Konto) bzw.
  +Betrag (im Gegenkonto), auf beiden Seiten 0 – auch nicht ganz kontiert –, bei jeder anderen Form außer den Spiegeln ihr
  **Betrag**, nicht ihre Wirkung (er trägt die Richtung der Geldbewegung; „2100 an 1460“ unter der Bewegung – WISO-Paar mit
  „1460 an Bank“, Kasse nicht auf 1460 – hat die Wirkung 0 und bliebe sonst Betriebsausgabe). *Übertrag* nur, wenn die Bewegung mindestens einen Spiegel zwischen eigenen Konten trägt (`is_mirror_row`, jede
  Spiegel-Form): Betrag minus Summe der Wirkungen aller übrigen Kinder; der Betrag des Spiegels wird nie gelesen, auch nicht
  bei `mirror_legacy`. Der Rest (Betrag − privat − Übertrag) geht nach dem Vorzeichen in Einnahme oder Ausgabe. Die Formel
  „Betrag − privat − Wirkung der übrigen Kinder“ ist falsch – sie zöge einen privaten Umbuchungssatz doppelt ab (−50 mit
  Spiegel −40, „6805 an Bank“ −10 und „2100 an 6805“ +10: Übertrag −40, privat −10, Ausgabe 0).
  **Bankbewegung ohne Kind:** Deckt die Übertragsrechnung des DATEV-Exports sie (`settle_transfers(…).covered`, über den
  ganzen Bestand; `db/matching.py` bleibt unberührt), ist sie ganz Übertrag, sonst nach dem Vorzeichen Einnahme oder
  Ausgabe; „offen“ (`unlinked_count`) bleibt sie in beiden Fällen – **außer bei einem Betrag von genau 0,00 € (seit 2.8.4,
  Entscheidung des Owners):** Eine solche Bewegung ohne Satz zählt nicht als offen (`Amount IS NOT 0`; `bank_count` zählt sie weiter), die
  Buchungsliste zeigt statt „offen“ das graue Abzeichen „0,00“ (`pages_transactions.is_zero_movement`, `ZERO_MOVEMENT_HINT`), und die
  Liquiditätsrechnung zählt sie nicht unter den Bankbewegungen ohne Buchungssatz; DATEV-Export und Summen- und Saldenliste übergingen sie
  schon vorher (`tests/test_zero_amount_movement.py`). Die Rechnung läuft nur, wenn eine solche Bewegung zu
  zählen ist, und einmal je `get_dashboard` (die Seite ruft nur das auf). **Eigenständiger Satz auf der Kasse** (Betrag aus
  Sicht der Kasse, `_cash_amount_sql`): ein Spiegel zwischen eigenen Konten (`is_mirror_row`, jede Form; `mirror_legacy` und
  Zeilen ohne Form mit dem Rohbetrag) ist Übertrag, 2100–2199 auf einer Seite privat, alles andere nach dem Vorzeichen.
  **Kontofilter:** Ein Übertrag auf ein nicht gewähltes eigenes Konto bleibt ein Übertrag und steht dann netto unter
  „Überträge“. Die Kassenseite eines Spiegel-Kindes unter einer Bankbewegung zählt weiter nicht (sie stünde doppelt da,
  sobald die Kasse die Bewegung selbst führt); ebenso bekannte Grenzen: der unkontierte Rest neben einem Spiegel steckt im
  Übertrag (auch ein Satz über 1460 ohne Wirkung auf das Konto), Konten ohne oder mit doppelter SKR-Nummer nehmen an der
  Deckung nicht teil, ein eigenständiger `mirror_legacy`-Spiegel auf der Kasse deckt die satzlose Bankseite nicht (er wird nie
  gedeutet: Ausgabe und Übertrag statt zweimal Übertrag), und die Deckung setzt wie beim DATEV-Export vollständige Auszüge
  voraus – fehlt die echte Gegenseite eines kontierten Übertrags im Bestand, kann eine gleich hohe Bewegung ohne Satz auf dem
  Zielkonto binnen 14 Tagen als Übertrag zählen, bis sie kontiert ist (`docs/aufgaben.md`). Beträge aus der
  Datenbank nur über `_minor_or_zero` (Text, NULL, Kommazahl ab 2^63: 0), ein Datum, das SQLite nicht liest, steht nur in den Summen – nie ein
  Fehler. Seite: Die Zeile „Überträge zwischen eigenen Konten“ (`.dashTransfers`) steht in der Übersicht nur bei einem Wert
  ≠ 0 (auf den Cent), im Monats-Tooltip nur bei ≠ 0, ohne Balken; Werte maskiert. `tests/test_dashboard_split.py` pinnt die
  Zerlegung (Regeltabelle, Eigenschaftsprüfung, feste Werte der Testwelt); der Maßstab von 2.0.8
  (`entry_form_golden_2_0_8.json`) gilt für das Dashboard nur über Saldo, Anzahl und Monatssummen
- **Betriebswirtschaftliche Auswertung (seit 2.5.14, `db/bwa.py`, `server/pages_bwa.py`, `export/pdf_bwa.py`; `GET /bwa`, `GET /bwa/pdf`):** eine
  reine Umgruppierung von `get_euer_data` ohne Kontofilter – sie übernimmt die Zahlen der EÜR unverändert und hat keine eigene Lesart der
  Buchungen. Einnahme, Ausgabe und „nicht im Ergebnis“ entscheidet EINE Funktion, `reporting.euer_section`, die auch die EÜR-Seite des
  Dashboards nutzt (`tests/test_bwa.py` hält die frühere Regel dieser Seite für alle Kontonummern fest). Die Gruppen stehen als Daten in
  `db/bwa.py` (`RANGES`, `EXCEPTIONS`, `group_of`); vom Nutzer pflegbare Gruppen gibt es nicht (das wäre eine Schemaänderung). Ein Konto ohne
  Gruppe steht unter „Nicht zugeordnet“ und zählt im Ergebnis wie in der EÜR – nie still in einer anderen Gruppe. Die Konten 3800–3849 außer 3806
  gehen je Konto und Zeitraum nach dem Vorzeichen ihres Saldos zu Erstattungen (> 0, unter den Betriebseinnahmen) bzw. Zahlungen. Invarianten
  (Test, auf den Minor-Wert, Testwelt und Demobestand): Ergebnis = EÜR-Ergebnis; Betriebseinnahmen − Erstattungen = Summe der EÜR-Einnahmen;
  Betriebsausgaben − Erstattungen = −Summe der EÜR-Ausgaben. Gerechnet wird nur in Minor Units, Prozente entstehen erst bei der Ausgabe: Ein
  Anteil bleibt leer bei einer Bezugsgröße ≤ 0, die Veränderung in % bei einem Vorjahreswert von 0 (und bei einem Quotienten, der den Decimal-Kontext sprengt). Gerundet wird erst
  bei der Anzeige: Trägt ein Bestand Beträge mit Bruchteilen eines Cents, kann eine gedruckte Summe um wenige Cent von der Summe der gedruckten
  Zeilen abweichen (wie in der EÜR) – nicht glätten, die Gleichheit mit der EÜR geht vor. Zwei Konten gleicher Nummer (verschiedene
  Kontenrahmen) stehen im Kontennachweis als eine Zeile unter dem zuerst gefundenen Namen. **Zeitraum und Darstellung:** der Monat und
  aufgelaufen ab 1. Januar. Die Seite nimmt den gemeinsamen Zeitraum der Seiten (`from`/`to` in der Adresse, sonst das Cookie, sonst das laufende
  Jahr – `resolve_period`; das Cookie setzt die Route wie bei jeder Seite mit Zeitraum) und leitet ihren Monat daraus ab (`period.period_month`, siehe
  *Dritte Menüzeile*); `year` und `month` liest sie nicht mehr (eine Adresse der alten Form ist eine Adresse ohne Zeitraum), einen Hinweis „ungültig“
  gibt es nicht – ein ungültiger Zeitraum wird wie überall verworfen. Das PDF liest `year`, `month` und `variant` streng (`parse_year`, `parse_month`,
  `parse_variant`; ein doppelter Schlüssel ist ungültig, 400 bei einem Fehler) und setzt kein Cookie; die Darstellung ist der Vergleich mit dem
  Vorjahr (Standard, auf der Seite auch bei ungültiger Angabe) oder `variant=jahr` (Monat und Jahr mit Anteilen), ihre Auswahl steht im Inhalt (kleines
  GET-Formular mit den Feldern `from`/`to`, kein „Anzeigen“-Knopf). Das PDF ist A4 quer, mehrseitig
  und landet nie auf der Platte (Download über `appDownload`; Dateiname `BWA_JJJJ-MM.pdf`, mit Anteilen `…_Anteile.pdf`). `/bwa` steht in der
  Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/bwa/pdf` nicht. Fester Hinweis: keine Steuerberatung.
  **CSV (seit 2.6.8, `export/csv_bwa.py`, `GET /bwa/csv`):** dieselben Zeilen und Zellen wie die Seite (`cells`), auf Wunsch mit Konten; UTF-8 mit BOM, Semikolon,
  Dezimalkomma ohne Tausenderpunkte, Prozentwerte ohne Zeichen; die Adresse wird streng gelesen wie beim PDF, nichts landet auf der Platte. Texte aus der
  Datenbank gehen nur über `csv_table.text_cell` (Steuerzeichen weg, höchstens 200 Zeichen, ein `'` vor `=`, `+`, `-`, `@` – auch hinter
  jedem Komma des Textes, falls jemand die Datei mit dem Komma als Trenner öffnet, und auch vor den breiten Schreibweisen dieser Zeichen (verglichen wird
  die Grundform, NFKC); sonst führte eine Tabellenkalkulation einen Kontonamen als Formel aus; nicht aufweichen).
  `tests/test_bwa.py`, `tests/test_bwa_pdf.py`, `tests/test_report_csv.py`
- **Vorsteuer in der EÜR (seit 2.8.0, `get_euer_data`):** Die Vorsteuer aus einer Ausgabe steht für JEDEN Satz in der EÜR – bis 2.7.0 fehlte sie bei jedem Satz außer 5, 7, 16 und 19 %
  (das Ergebnis war um sie zu hoch). Reihenfolge: (1) wie bisher 1401 (5/7 %) bzw. 1406 (16/19 %), der Vergleich `abs(Satz − x) < 0,0001` wörtlich, wenn der Kontenrahmen das Konto
  führt; (2) ein als Prozentzahl gespeicherter Satz über die feste Tabelle der Summen- und Saldenliste; (3) das Vorsteuerkonto des ersten Stammdaten-Eintrags mit Konto für diesen
  Satz (`db.tax_rates.select_report_tax_accounts`: nach Reihenfolge und ID, aktiv oder nicht; ungültige Zeilen, Satz 0, die festen vier Sätze und Konten außerhalb des Bereichs
  zählen nicht); (4) sonst die rechnerische Zeile **1409 „Vorsteuer ohne Steuerkonto“** (`reporting.EUER_INPUT_TAX_UNASSIGNED`, ab 1 Cent, Gruppe „Gezahlte Vorsteuer“ der BWA) – auch
  für einen Steuerbetrag ohne Satz und für 19/7 % bei gelöschtem 1406/1401. Die Umsatzsteuer bleibt EINE virtuelle Zeile 3806 (das Umsatzsteuerkonto eines Eintrags wirkt nur in der
  Summen- und Saldenliste). **Gelesen wird der Stand der Stammdaten von heute:** Wer einem Eintrag ein Steuerkonto gibt oder nimmt, verschiebt die Steuer auch früherer Zeiträume
  zwischen Sammelzeile und Konto; Summen und Ergebnis bleiben gleich. Für Bestände ohne solche Ausgaben ändert sich kein Zeichen (alle Schnappschüsse bleiben). Bekannte Grenzen: Die
  Einordnung 6884–6892 (EÜR liest den Steueranteil als Vorsteuer, die Liste als Umsatzsteuer) gilt jetzt für jeden Satz; Zweckkonten, die kein Aufwand sind (Privat, Darlehen), führen
  ihre Steuer wie bisher bei 19 % als Vorsteuer. `tests/test_tax_accounts_reports.py`
- **EÜR-Ansicht (seit 2.5.16, `db/euer.py`, `server/pages_euer.py`, `export/pdf_euer.py`; `GET /euer`, `GET /euer/pdf`):** EINE Aufbereitung für den
  Kasten der EÜR im Dashboard und die Seite (`db.euer.euer_statement`: die Zeilen von `get_euer_data` nach `reporting.euer_section` in Einnahmen,
  Ausgaben und „Sonstige Ausgaben“; Summen aus Minor Units, nie als Summe von `float`; die echte und die virtuelle 3806 bleiben zwei Zeilen;
  Ergebnis = Einnahmen + Ausgaben, die „Sonstigen Ausgaben“ zählen nicht mit). Der Kasten im Dashboard bleibt **zeichengleich** – bis auf die eine
  Zeile mit dem Link „zur Auswertung“ unter der Überschrift (mit dem Zusatz „(dort ohne Kontofilter)“, wenn dort Konten gewählt sind): SHA-256-Schnappschüsse
  am Stand 2.5.14 (`tests/test_euer_view.py`); ändert sich ein Zeichen, ist der Code zu ändern, nie der Schnappschuss. Die bekannten Abweichungen
  von 2.5.14: Ein Ergebnis von genau 0 ist grün (der Kasten rechnete die Summen als `float`; ein Rest von −5,55e-17 färbte das „0,00 €“ rot), und
  bei Summen ab rund 6·10¹¹ € kann der letzte Cent anders – nämlich richtig – gerundet sein (die `float`-Summe verlor dort Stellen); die Zeilen
  selbst sind immer gleich. Ein leerer Wert in der Adresse (`from=`) gilt als nicht angegeben (`parse_qs` lässt ihn fallen, wie bei `/bwa`). Die Seite
  hat **keinen Kontofilter** (mit Filter fielen das Verrechnungskonto 1460 und die AfA weg, es wäre keine EÜR mehr). Die Seite nimmt den gemeinsamen
  Zeitraum der Seiten (`from`, `to` in der Adresse, sonst das Cookie, sonst das laufende Jahr – `resolve_period`): Ein ungültiger Wert wird wie überall
  verworfen, ohne Hinweis, ein Paar mit `from` nach `to` getauscht (`valid_period`), das Cookie setzt die Route wie bei jeder Seite mit Zeitraum; ihre
  Menüzeile ist `period_filter_widget` (die fünf Jahresknöpfe rechnen aus dem `today`, das die Route übergibt). Das PDF liest den Zeitraum weiter streng
  (`period.parse_iso_date`): beide sind nötig, `from` liegt nicht nach `to` – **kein stiller Tausch** –, ein doppelter Schlüssel ist ungültig, 400
  „Ungültiger Zeitraum.“, kein Cookie. Das PDF ist A4 hoch, mehrseitig und landet nie auf der Platte
  (Download über `appDownload`; Dateiname `EUER_JJJJ.pdf` für ein volles Kalenderjahr, sonst `EUER_von_bis.pdf`); seine Bausteine (Text bereinigen,
  kürzen, umbrechen, Zelle, Linie, Seitenverteilung) stehen in `export/pdf_table.py` und gelten auch für `pdf_bwa`, dessen Ausgabe dabei dieselbe blieb
  (Schnappschuss in `tests/test_pdf_table.py`). Fester Hinweis: kein amtliches Formular, keine Steuerberatung. **Untermenü der Auswertungen:** `REPORT_PAGES`
  und `reports_submenu` in `server/pages_bwa.py` (seit 2.6.3 mit der Liquidität als viertem Eintrag); jede Seite darunter ruft `Header1('bwa')` auf (der Menüpunkt bleibt markiert). `/euer` steht in der
  Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/euer/pdf` nicht. `tests/test_euer_view.py`, `tests/test_pdf_table.py`
- **Summen- und Saldenliste (seit 2.5.17, `db/susa.py`, `Database.get_susa`, `server/pages_susa.py`, `export/pdf_susa.py`; `GET /susa`, `GET /susa/pdf`):** je Konto
  Anfangssaldo, Soll und Haben im Monat und aufgelaufen seit dem 1. Januar, Endsaldo (`1.234,56 S`/`H`). **Sie liest dieselben Zeilen wie der Export für das Steuerbüro und
  hat keine eigene Lesart:** `accounts.LEDGER_ROWS_WHERE` ist das `WHERE` von `fetch_bookings_range` (alle Sätze außer den Bankbewegungen bis Monatsende, die Vorjahre bilden den
  Anfangssaldo), Form und Soll-Seite kommen aus `matching.ledger_form`/`coa_in_soll` – dieselben Funktionen, die `export_to_datev` ruft; die Auslagerung ließ den Export **Byte für Byte**
  gleich (SHA-256-Schnappschüsse in `tests/test_susa.py`, am Stand 2.5.16 vor der Auslagerung genommen: ändert sich ein Byte, ist der Code zu ändern, nie der Schnappschuss) –,
  die Art aus `datev_keys.booking_kind`, die Wartekonten aus `get_waiting_account_numbers`; dazu fehlen die Eingangs-Sätze gepaarter Überträge (`settle_transfers(…).omitted`: der Ausgangs-Satz bucht
  beide Bankseiten) und die gedeckten Bankbewegungen ohne Satz (`.covered`). Ein Test hält für JEDE Zeile der Testwelt und des Demobestands fest, dass die Liste dieselbe Seite bucht wie die Zeile des
  Exports (Konto, Gegenkonto, S/H, Betrag). **Buchungsregel** (`entry_postings`, rein, in Minor Units): Betrag ohne Vorzeichen, Soll/Haben nach der Form (`purpose_first`: positiv Gegenkonto im Soll,
  `liquid_first`/`transfer`/`mirror_coa`: positiv Konto im Soll, `mirror_counter`: positiv Gegenkonto im Soll); `mirror_legacy` bucht nie (Lesart unbekannt – wie beim Abgleich nie gedeutet, Hinweis); der
  **gespeicherte** Steuerbetrag wird aus der Seite des Zweckkontos herausgerechnet (nichts nachgerechnet) und steht auf 1401/1406 (Vorsteuer) bzw. 3801/3806 (Umsatzsteuer; 16 % und 5 % auf den Konten von 19 % und 7 %,
  `TAX_ACCOUNTS`; ein anderer Satz seit 2.8.0 auf dem Umsatzsteuer- bzw. Vorsteuerkonto, das die Stammdaten für ihn führen (`SusaContext.tax_accounts`, die feste Tabelle gilt zuerst und der Export kennt diese Konten nicht), sonst auf „Steuer ohne Steuerkonto“; ein Steuerkonto, das der Kontenrahmen nicht führt, mit dem Zusatz „(nicht im Kontenrahmen)“ – der Standard-Kontenrahmen führt 3801 seit 2.6.1,
  ein älterer Bestand bekommt es über „fehlende Standardkonten“ und trägt bis dahin den Zusatz), die Seite bestimmt `tax_side` nach der Art; die
  Forderung einer Rechnung und die Gegenbuchung der Stornorechnung (Debitor auf einer Seite) werden nie zerlegt. **Kontenklassen und Vortrag stehen an EINER Stelle** (`GROUPS`, `group_key`, `is_carried`):
  Bestandskonten und die Wartekonten 4340/4345/4405 (eigene Gruppe „Offene Posten“) tragen vor, Erfolgskonten 4000–7999 beginnen bei 0 – ihre Summe der Vorjahre steht auf dem virtuellen Konto „Ergebnis der
  Vorjahre (rechnerisch)“ beim Eigenkapital; Privat- und Steuerkonten werden mangels Abschluss vorgetragen, eine Eröffnungsbilanz gibt es nicht (ein Konto ohne Buchungen vor dem ersten Kontoauszug beginnt bei 0).
  **Ausgleich:** Jede Buchung schreibt gleich viel ins Soll wie ins Haben, was fehlt, steht auf dem virtuellen Konto „Nicht kontiert“ (eine Seite ohne Konto oder Nummer, die Gegenseite einer Bankbewegung ohne Satz, die
  kein Übertrag deckt): Summe Soll = Summe Haben je Spalte, Summe der Anfangs- und der Endsalden 0. Ein Satz unter einer Bankbewegung ohne Geldseite (`purpose_first` ohne Gegenkonto, `liquid_first` ohne Konto)
  zählt für das Konto der Bankbewegung (Hinweis; ohne Nummer: „Nicht kontiert“), eine Zeile ohne Form wird wie im Export mit den Zahlungskonten von heute gelesen (Hinweis). Was sich nicht buchen lässt
  (`mirror_legacy`, ein Betrag, der keine ganze Zahl bis 10^15 Minor Units ist, ein Datum, das kein Kalendertag ist, eine Zeile ohne Buchungsart, ein Satz mit Betrag 0 und Steuerbetrag), steht nur in den **Hinweisen** (`NOTICE_TEXTS`, Einzahl und Mehrzahl) – nie ein Fehler, nie still; auch die
  Bankbewegungen mit unstimmigen Sätzen (`unbalanced`) rechnet `get_susa` mit den lesbaren Beträgen selbst (`_susa_unbalanced`, dieselbe Regel `bank_effect` und Auswahl wie `find_unbalanced_splits`, das bei einem Betrag,
  der Text ist, mit einem Fehler abbricht). **Gültiges Datum** ist ein Kalendertag als Text `JJJJ-MM-TT` ab dem Jahr 0001 (`_SUSA_DATE_OK`: `typeof = 'text'`, das Muster und `x IS date(x)` – das Muster hält die Form unabhängig von der SQLite-Version fest; SQLite rückt `2026-02-30` auf einen echten Tag, kennt `Dezember` nicht und liest die Zahl
  `20260305` nicht als Text): EINE Bedingung für die gebuchten Sätze und die Bankbewegungen ohne Satz. `bad_date` zählt jede Zeile mit Buchungsart und ungültigem Datum, auch eine Bankbewegung, **ohne Fenster** (sie fehlt in jedem Monat, auch im
  Vortrag), `unknown_type` die Zeilen ohne Buchungsart (`BookingType` NULL: die Bedingung `!= 'bank'` des Exports trifft sie nie, er liest sie so wenig wie die Liste), `not_readable` nur Zeilen mit gültigem Datum bis zum Monatsende – jede Zeile steht in
  genau einem dieser Hinweise (Reihenfolge: Buchungsart, Datum, Betrag); eine andere Buchungsart als `entry` und `bank` liest der Export wie einen Satz, und so bucht sie die Liste. Ein Satz mit **Betrag 0 und Steuerbetrag** (der Bank-Editor lässt ihn zu)
  bucht nichts – ohne Betrag gibt es keine Seite –, `zero_amount_tax` nennt ihn mit der Summe der Steuer. **Abstimmungsproben** (Pflicht-Tests, auf den Minor-Wert): (a) Summe Soll = Summe Haben, Anfangs- und Endsalden zusammen 0 – in jedem Bestand und jedem Monat, auch in
  Beständen aus Zufall (fester Seed); (b) jedes eigene Bankkonto (keine Kasse, eigene SKR-Nummer) hat als Endsaldo die Summe aller Bankbewegungen bis Monatsende (nach dem Datum als Text: Eine Bewegung, deren ungültiges Datum vor dem Monatsende sortiert, zählt mit,
  fehlt in der Liste und steht unter `bad_date`) – steht als „Abgleich mit den Bankbewegungen“ auf der Seite;
  es geht auf, wo jede Bewegung kontiert ist, jeder Übertrag beide Seiten am selben Tag trägt, alle Beträge lesbar sind, jeder Satz mit seiner Bankbewegung auf derselben Seite des Monatsendes liegt (sonst `child_other_period`: um den Satz kann der Abgleich abweichen – gezählt wird jeder solche Satz, auch einer ohne Wirkung auf das Bankkonto) und
  jeder Satz unter einer Bewegung auf das Konto der Bewegung bucht (sonst `child_other_account`: ein gebuchter Satz, dessen Konto oder Gegenkonto ein anderes eigenes Bank- oder Kassenkonto ist – ein Spiegel zwischen eigenen Konten zählt nicht, er
  berührt das andere Konto absichtlich; beide Hinweise nur gezählt, an der Buchung ändert sich nichts); (c) die Erfolgskonten gegen die EÜR (für jede Zeile eines EÜR-Abschnitts außer 3806 der Saldo der EÜR =
  Haben − Soll der Liste, die Umsatzsteuer der EÜR = 3801 plus 3806) – es geht auf, wo jeder Satz einer Bankbewegung oder der Kasse zugeordnet ist; Sätze ohne Bankbewegung, Umbuchungen ohne Zahlung und Status `resolved`
  zählt die Liste nach ihrem Datum, die EÜR nicht. **Laufzeit:** Die Sätze werden in SQL nach dem gruppiert, was ihre Lesart bestimmt (Form, Konten, Richtung des Betrags und der Steuer, Satz, Zeitraum, Konto der Bankbewegung), und je Gruppe
  einmal gebucht (`book_group`, die Regel ist linear) – nie wieder eine Zeile je Satz in Python; reicht eine Summe über 2^63 Minor Units hinaus (SQLite wirft `integer overflow`), rechnet `reporting._sum_groups` dieselbe Abfrage in Python (jeder andere
  Datenbankfehler geht weiter), und ein Name, der kein Text ist (Blob, NULL), wird als Text gelesen (`_name_text`). Die Seite nimmt den gemeinsamen Zeitraum (`from`/`to` in der Adresse, sonst das Cookie, sonst das laufende Jahr – `resolve_period`; das Cookie
  setzt die Route wie bei jeder Seite mit Zeitraum) und leitet ihren Monat daraus ab (`period.period_month`); `year` und `month` liest sie nicht mehr, einen
  Hinweis „ungültig“ gibt es nicht. Sie schreibt nichts in den Bestand; das PDF liest `year` und `month` streng (`parse_year`/`parse_month`, ein doppelter
  Schlüssel ist ungültig, 400) und setzt kein Cookie (A4 quer, mehrseitig, `Summen_und_Salden_JJJJ-MM.pdf`, landet nie auf der Platte). Texte ohne Marken- und Produktnamen; fester Hinweis: keine Steuerberatung, ohne Zusicherung der
  Richtigkeit. **Diese Regeln nicht still ändern.** `/susa` steht in der Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/susa/pdf` nicht.
  **CSV (seit 2.6.8, `export/csv_susa.py`, `GET /susa/csv`):** je Konto und Gruppe die sechs Werte immer als Zahl, Salden als Betrag mit eigenem Kennzeichen S/H, am Ende
  die Gesamtsumme; Hinweise und Bankabgleich stehen nur auf Seite und PDF.
  `tests/test_susa.py`, `tests/test_susa_pdf.py`, `tests/test_report_csv.py`
- **Liquiditätsrechnung (seit 2.6.3, `db/liquidity.py`, `Database.get_liquidity`, `server/pages_liquidity.py`, `export/pdf_liquidity.py`; `GET /liquiditaet`, `GET /liquiditaet/pdf`):** vom Ergebnis der
  BWA zur Veränderung der flüssigen Mittel – ein Monat und aufgelaufen seit dem 1. Januar, im Vorjahresvergleich (Standard, acht Wertspalten) oder als „Monat und Jahr“ (`variant=jahr`, zwei
  Spalten, **keine Anteile**: ein Anteil an Erlösen ergäbe bei Liquidität keinen Sinn). **Sie hat keine eigene Lesart der Buchungen:** Gerechnet wird nur mit den Zeilen von `get_euer_data` (ohne Kontofilter,
  eingeordnet von `euer_section`; je Spanne ein Aufruf mit allen Zeilen und – seit 2.6.4 – einer mit `only_asset_transfers=True`: nur die Zeilen aus den Umbuchungen
  zwischen Anlage- und Erfolgskonto, die kein Geld bewegen: die Sätze, die Sektion 3 der EÜR beisteuert, und die Umbuchungen dieser Art UNTER einer Bankbewegung (Sektion 1, `is_asset_transfer`: der Restbuchwert im Split eines
  Verkaufs, eine dort zugeordnete AfA – ihre Wirkung auf das Konto der Bewegung ist 0), jeder Satz mit dem, was er im vollen Lauf beisteuert; ein Satz, den Sektion 2 oder 2b findet, gehört nicht dazu – dieselben Abfragen, dasselbe
  Aufsummieren, keine neue Lesart; der Standard ohne den Schalter ist unverändert), dem Bericht der BWA (`build_report`)
  über die vollen Zeilen und den Töpfen des Dashboards (`_dashboard_movements`: Einnahme, privat, Ausgabe, Übertrag – je Spanne EIN Aufruf,
  nie ein Lauf über das Jahr, der nach Monat aufgeteilt wird; so ist Einnahme + privat + Ausgabe + Übertrag auf den Minor-Wert gleich der Karte `get_dashboard(…)['balance']` derselben Spanne). Die einzige neue Abfrage
  ist `_liquidity_unbooked` (Bankbewegungen ohne Buchungssatz, die kein Übertrag deckt – dieselbe Auswahl wie der Zweig „ohne Kind“ des Dashboards, Beträge über `_minor_or_zero`, die Übertragsrechnung
  `_settle_stock` nur, wenn eine solche Bewegung einen ganzzahligen Betrag ≠ 0 hat, und höchstens einmal je Aufruf). Vorhandene Funktionen wurden nicht umgebaut (`get_euer_data` bekam nur den Schalter, sein Standard bleibt
  Zeichen für Zeichen gleich); die Schnappschüsse (BWA-PDF, EÜR-Kasten, Export der
  Summen- und Saldenliste) bleiben unverändert. **Zeilen** (Minor Units, jeder Wert mit seiner Wirkung auf die flüssigen Mittel, Zufluss +; `R` = Saldo der EÜR-Zeile je Konto, `U` = Saldo des Kontos in den Umbuchungen
  zwischen Anlage- und Erfolgskonto, `get_euer_data(…, only_asset_transfers=True)`): (1) Ergebnis = das der BWA; (2) Abschreibungen = −Σ U der Konten der BWA-Gruppe `depreciation` (6200–6299); (3) Anlagenabgänge zum
  Restbuchwert = −Σ U der Konten 4855–4858 und 6895–6898 (`DISPOSAL_RANGES`); (3a) Übrige Umbuchungen ohne Geldfluss = −Σ U jedes anderen Kontos (steht nur bei einem Wert). **Seit 2.6.4 rechnet die Überleitung nur zurück,
  was die EÜR als Umbuchung zwischen Anlage- und Erfolgskonto liest** (AfA, Restbuchwert beim Abgang): Eine EÜR-Zeile allein füllt die Zeilen 2, 3 und 3a nie, und eine direkt bezahlte Abschreibung („6260 an Bank“) bewegt
  Geld, bleibt im Ergebnis und braucht keinen Rest. Die Prüfung geht erst nach `DISPOSAL_RANGES`, dann nach der Gruppe `depreciation`, sonst nach 3a (`_noncash_line`); im Kontennachweis steht der Betrag der Umbuchungen,
  nicht der ganze Saldo, den eine Zahlung auf demselben Konto mitträgt. (4) Darlehen und Verbindlichkeiten im Ergebnis = BWA-Gruppe `liabilities` (3000–3799 außer 3160, 3720, 3740),
  herausgerechnet; (5) Zwischensumme „Ergebnis vor Abschreibungen und Darlehen“ (1 + 2 + 3 + 3a + 4); (6) Anlagenzugänge = Σ R der Konten unter 1000 außerhalb des Ergebnisses; (7) Darlehen und Verbindlichkeiten = Σ R aller Konten 3000–3799
  (Aufnahme +, Tilgung −; die Zeilen 4 und 7 heben sich bis auf 3160, 3720 und 3740 auf); (8) Privatentnahmen 2100–2179; (9) Privateinlagen 2180–2199; (10) übrige Konten außerhalb des Ergebnisses (erwartet 0, steht nur bei einem Wert);
  (11) Veränderung laut Überleitung = 5 bis 10; (12) Bankbewegungen ohne Buchungssatz (die Bezeichnung nennt ihre Zahl im Monat und aufgelaufen); (13) **Nicht zugeordnet** = Ist − (11 + 12), steht immer da, auch bei 0,00;
  (14) Veränderung der flüssigen Mittel („Ist“) = Einnahme + privat + Ausgabe aller Bank- und Kassenkonten **ohne Überträge zwischen eigenen Konten** (Geld zwischen eigenen Konten ändert die flüssigen Mittel nicht); die Überträge
  und die Veränderung laut Kontobewegungen stehen nur nachrichtlich und nur, wenn Überträge dastehen. Die Bereiche stehen als Daten an EINER Stelle (`DISPOSAL_RANGES`, `FINANCING_RANGE`, `WITHDRAWAL_RANGE`, `DEPOSIT_RANGE`,
  `ASSET_LIMIT`) und gelten nur für ganzzahlige Kontonummern (eine Kommazahl aus der SQL-Konsole hat wie in `bwa.group_of` keinen Bereich); **jede EÜR-Zeile steht an genau einer Stelle** (ein Test je Grenze der Bereiche), zwei Zeilen gleicher Nummer werden je Zeitraum addiert und stehen als EINE Zeile unter dem zuerst gefundenen Namen.
  **Abstimmung:** „Nicht zugeordnet“ ist per Definition die Differenz und keine dritte Lesart – was die Überleitung aus den Buchungen nicht erklärt, bleibt dort sichtbar und wird in der Überleitung nie einer anderen Zeile zugeschlagen. **Grenze der Ist-Seite** (sie ist die des Dashboards): Trägt eine
  Bankbewegung einen Spiegel zwischen eigenen Konten, steckt der Teil ihres Betrags, den kein Satz erklärt, im Übertrag – ein unstimmiger Split neben einem Übertrag steht deshalb nur nachrichtlich unter den Überträgen,
  nicht in „Nicht zugeordnet“ (Test mit festen Werten; die Prüfung der Splits nennt die Bewegung). „Geht auf“
  heißt exakt 0 Minor Units; ein Rest unter einem halben Cent steht als 0,00 da und wird auf der Seite als Rundungsrest genannt, nie geglättet. Pflicht-Tests (`tests/test_liquidity.py`): Im Demobestand und in Beständen aus Zufall über
  Wege der App (`tests/liquidity_helpers.random_books`) ist sie in jedem Monat und aufgelaufen 0; in der SuSa-Welt (März +20,00, ab März aufgelaufen) und der Testwelt (April −70,00, Mai −55,00, Juni −1,00) stehen feste Werte als Maßstab –
  an ihnen darf sich nichts verschieben; für jeden benannten Fall ist festgehalten, was dort landet: ein Satz über das Verrechnungskonto 1460 in einem anderen Monat als seine Bankseite, ein Satz mit anderem Datum als seine Bankbewegung über eine
  Monatsgrenze, ein unstimmiger Split, ein Steuersatz außer 5, 7, 16 und 19 % (seit 2.8.0: seine Vorsteuer steht in der EÜR auf dem Stammdaten-Konto bzw. auf „Vorsteuer ohne Steuerkonto“ und nicht mehr in „Nicht zugeordnet“ – der feste Wert des Tests ist seitdem Ergebnis −110 statt −100), der Status `resolved`, eine Zahlung auf ein Bestandskonto ohne Gegenumbuchung,
  eine Umbuchung Anlage ↔ Erfolgskonto mit der Kasse als `Account_ID`, ein Spiegel alter Art mit 1460 und ein Rundungsrest. Es gehen auf (je ein Test, auch ihr Kontennachweis): die direkt bezahlte Sofortabschreibung
  „6260 an Bank“ (bleibt im Ergebnis; auch neben der AfA der App im selben Monat und auf demselben Konto 6220), die Umbuchung „6815 an 0520“ (Zeile 3a), ein Abgang über das Verrechnungskonto („6895 an 1460“ und „1460 an 0520“:
  Aufwand und Anlagenzugang heben sich auf) und die Umbuchung unter einer Bankbewegung (der Restbuchwert im Split des Verkaufs, „0670 an Bank“ mit „6260 an 0670“ in einem Beleg, eine im Editor zugeordnete AfA; trägt die Bewegung NUR
  die Umbuchung, bleibt ihr Betrag als unstimmiger Split in „Nicht zugeordnet“). Dazu prüft ein Test die Zerlegung von `get_euer_data(…, only_asset_transfers=True)`: Saldo(alle) − Saldo(nur Umbuchungen) = Saldo eines Bestands, aus dem genau diese Sätze gelöscht
  sind. **Diese Erwartungen nicht lockern** – ändert
  sich ein Wert, ist die Ursache zu klären. **Seite:** Untermenü der Auswertungen (`REPORT_PAGES`, vierter Eintrag „Liquidität“), `Header1('bwa')`; Zeitraum und Darstellung wie bei der BWA: Die Seite nimmt den gemeinsamen Zeitraum (`resolve_period`,
  das Cookie wie überall; ihr Monat folgt aus `period.period_month`) und liest aus der Adresse nur `variant` (`pages_bwa.read_variant`: unbekannt oder doppelt → Standard, ohne Hinweis); das PDF liest `year`, `month` und `variant`
  streng (`pages_bwa.read_pdf_query`: 400 bei einem Fehler) und setzt kein Cookie; nichts wird in den Bestand geschrieben; die Seite nutzt nur
  vorhandene Klassen (`bwaTable` …), ihr Skript nur `var` und `function` mit dem Präfix `liq`; unter den Zeilen mit Konten lassen sich diese aufklappen. Fester Hinweis: „Aus den Buchungen dieser App gerechnet. Keine Steuerberatung; ersetzt
  nicht die Auswertung eines Steuerbüros.“ – ohne Marken- und Produktnamen. **PDF:** A4 quer, mehrseitig, nie auf der Platte (Download über `appDownload`; `Liquiditaet_JJJJ-MM.pdf`, in der Darstellung „Monat und Jahr“ `…_Monat_Jahr.pdf`), auf Wunsch mit
  Kontennachweis, hinter der Tabelle ein Textblock mit dem Satz zur Abstimmung; seine Bausteine stehen in `export/pdf_table.py`. `/liquiditaet` steht in der Positivliste des Diagnose-MCP (es schreibt nichts in den Bestand, Nachweis im Test), `/liquiditaet/pdf` nicht;
  die Routen stehen in keiner der Mengen `_ACTION_PATHS`, `_JSON_PATHS` und `_LOCK_FREE_ROUTES`. **Laufzeit** (gemessen an einem Bestand mit 200 000 Buchungssätzen): rund 2,5 s im Vorjahresvergleich (vier Spannen), rund 1,4 s als „Monat und Jahr“,
  im Demobestand `full` rund 10 bis 20 ms; je Spanne zwei Aufrufe von `get_euer_data` (alle Zeilen und nur die Umbuchungen, je rund 0,1 s); den größten Teil macht die Übertragsrechnung (`_settle_stock`, liest den ganzen Bestand,
  je Aufruf rund 0,3 s) aus – `_dashboard_movements` ruft sie je Spanne, die Liquiditätsrechnung selbst nur einmal je Aufruf.
  `tests/test_liquidity.py`, `tests/test_liquidity_pdf.py`, Hilfen `tests/liquidity_helpers.py`
