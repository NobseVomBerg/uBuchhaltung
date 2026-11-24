# Arbeitszeiten, Fahrtenbuch, Stundenzettel

## Arbeitszeiten und Fahrtenbuch

- **Arbeitszeit rechnet in Minuten** (`server/pages_worktime.py`: `clock_minutes`, `compute_minutes`, `crosses_midnight`,
  `format_hours`): Uhrzeiten gelten nur als `HH:MM` aus ASCII-Ziffern (0–23, 0–59), sonst zählt der Eintrag 0 Minuten und wird
  beim Speichern abgelehnt („Bitte die Uhrzeit als HH:MM angeben.“). **Ende vor Beginn = Folgetag** (+ 24 h; Ende = Beginn sind
  0 Minuten); der Eintrag zählt ganz zu seinem Datum und trägt in Liste und PDF „(+1)“. **Summen entstehen aus Minuten, nie aus
  den gerundeten Stunden der Zeilen** (`compute_hours` ist nur die Zeilenanzeige); „Arbeitstage“ sind Kalendertage mit Arbeit
  über 0 Minuten – auf der Seite und im Stundenzettel gleich. Die Überlappungsprüfung (`_wt_has_overlap`) rechnet auf der
  Zeitachse des Eintragsdatums (ein Eintrag über Mitternacht reicht dort über 24:00 hinaus), die Nachbartage prüft sie nicht.
  Die Vorschau im Formular (`wtUpdateHours`) rechnet dieselbe Regel und nennt „Ende am Folgetag“
- **Fahrtenbuch bleibt locker** (Entscheidung des Owners): einzelne Fahrten, ohne Tachostand, in beliebiger Reihenfolge
  nachgetragen – lückenlos muss es nur für die Fahrtenbuchmethode sein. **Beim Speichern wird nie ein Anschluss an die
  Vorfahrt geprüft, weder als Fehler noch als Hinweis – nicht einführen.** Abgelehnt wird nur Unmögliches, an EINER Stelle
  (`handlers._trip_fields`, reine Funktion für Anlegen und Ändern): Fahrer/Start-Datum/Ziel fehlen, Datum kein ISO-Datum,
  Ende vor Start, Uhrzeit nicht `HH:MM`, Kilometer keine ganze Zahl von 0 bis 9 999 999, Tacho Ende unter Tacho Start,
  ungültiger Beleg, Text über 500 Zeichen. Die Meldung geht als `?error=` auf die Seite (`script_json` + `appMsg`), nichts
  wird gespeichert; das Formular prüft dasselbe vor dem Absenden (`trValidate`), der Server bleibt maßgeblich.
  `insert_trip`/`update_trip` melden einen Fehlschlag (`None`/`False`) – kein verschluckter Fehler. **Lückenprüfung nur auf
  Wunsch** (Kasten unter der Liste, GET `/trips?…&check_vehicle=&check_year=`, rein lesend): `db.trips.logbook_gaps`
  (reine Funktion) über alle Fahrten eines Fahrzeugs im Kalenderjahr, alle Fahrer, Fahrzeug nach `strip()` + `casefold()`.
  Lücke = Fahrt mit Tacho Start, deren nächster Vorgänger mit Tacho Ende einen anderen Stand hat; Fahrten ganz ohne
  Tachostand dazwischen werden gezählt (`between`), nach einem Start ohne Ende ist der Stand unbekannt (kein Vorgänger –
  dieselben Kilometer stehen nie in zwei Einträgen); höchstens 100 Einträge plus Zahl. Der Hinweistext zur
  Fahrtenbuchmethode steht fest auf der Seite (keine Steuerberatung). Eine gespeicherte Einstellung je Fahrzeug gibt es
  nicht – Fahrzeuge sind Freitext an der Fahrt, eine Stammliste wäre eine Schemaänderung
- **„Als neu kopieren“ (seit 2.5.3, Arbeitszeiten und Fahrten, wie bei der Rechnung):** Der Knopf steht nur an einem geladenen
  Eintrag und **speichert nichts** (`type="button"`, `wtCopyAsNew`/`trCopyAsNew`): Er leert die ID im Formular, stellt es auf
  „neu“ und setzt das Datum auf heute; angelegt wird erst mit „Speichern“ über `/worktime/add` bzw. `/trips/add` mit allen
  Prüfungen. Arbeitszeit: alle Angaben bleiben, die Vorlage des letzten Eintrags füllt eine Kopie nicht auf (`wtIsCopy`).
  Fahrt: Strecke, Fahrzeug, Grund, Uhrzeiten und Kilometer bleiben, eine mehrtägige behält ihre Dauer; Tachostände und Beleg
  bleiben leer (sie gehören zu der einen Fahrt). Das frühere „Als neu speichern“ der Fahrten (legte sofort an) gibt es nicht
  mehr. `tests/test_copy_as_new.py`
- **Stundenzettel-PDF** (`export/pdf_worktime.py`): mehrseitig über `build_multi_page_pdf`; `page_layout(row_count)` verteilt die
  Zeilen – keine Datenzeile unter `BOTTOM_Y`, Summe und Unterschriften zusammen auf der letzten Seite (notfalls eine eigene
  Seite ohne Datenzeilen), „Seite n von m“ nur bei mehr als einer Seite. Passt alles auf eine Seite, ist das Layout das bisherige.
  **Info-Spalte (seit 2.5.3):** Art des Tages, auf Wunsch der Arbeitsort eines Arbeitstags (`with_location`), Pausenzeiten, auf
  Wunsch die Notiz (`with_notes`), mit „ · “ verbunden. Die Seite hat dafür zwei Häkchen neben dem PDF-Knopf („Mit Notiz“, „Mit
  Arbeitsort“), **beide beim Laden an**; die Adresse nennt immer beide (`notes`, `location`: nur `1`/`true`/`on` schalten ein,
  ohne Angabe druckt der Server keines von beiden). Der Text **bricht in der Spalte um** (`info_lines`: am Wortrand, ein
  überbreites Wort mitten im Wort, Leerraum und Zeilenumbrüche als ein Leerzeichen) und endet nach `MAX_INFO_LINES` = 8 Zeilen
  mit „…“ – nie still abgeschnitten. Gemessen wird der Text, **wie er gedruckt wird** (`printed_text`: WinAnsi-Ersetzungen),
  mit den Breiten der eingebetteten Schrift (`info_width`, nicht die Helvetica-Tabelle – dort fehlen breite Zeichen), und
  **die Laufzeit hängt nur von den gedruckten Zeilen ab**: Jedes Zeichen wird höchstens zweimal gemessen, nach neun Zeilen
  hört das Lesen auf. Notiz, Pausentext und Art sind unbegrenzte Felder – nie wieder je Teilstück den ganzen Rest messen
  (ein Wort mit 8000 Zeichen kostete so 49 s). `page_layout` nimmt die Höhen der Zeilen in Textzeilen (oder wie bisher eine Zahl): Kein
  Eintrag wird über zwei Seiten geteilt, keine Textzeile steht unter `BOTTOM_Y` (`tests/test_worktime_pdf_info.py`)
- **Kundenfilter und leere Tage (seit 2.5.4, Arbeitszeiten-Seite und Stundenzettel):** Wer je Kunde abrechnet, braucht einen
  Zettel je Kunde; wer als Mitarbeiter seine Zeit nachweist, den Zettel über alles. **Die Regel steht an EINER Stelle**
  (`server/pages_worktime.py`): `customer_filter` liest den Wert streng (`''` = alle, `'none'` = ohne Kunde, sonst eine positive
  ASCII-Ganzzahl in 64 Bit, alles andere `None` – **auch Leerraum**: `customer=%20` ist kein leerer Wert), `entry_matches_customer` entscheidet je Eintrag, `customer_query` baut
  `&customer=…` für Adressen – Seite und PDF nehmen dieselbe. Zum Zettel des Kunden gehören seine Einträge **und die Einträge
  einer anderen Art als „Arbeit“ ohne Kunden** (Urlaub, Krank, Feiertag stehen auf jedem Kundenzettel – nur die Arten des
  Formulars, `KIND_LABELS`; eine unbekannte Art aus Altbestand oder SQL-Konsole ist keine Abwesenheit); Arbeitseinträge anderer
  Kunden und Arbeitseinträge ohne Kunde nie, der Urlaub eines anderen Kunden auch nicht. „ohne Kunde“ nimmt alle Einträge ohne
  Kunden, kein Filter alles. **Ungültig heißt auf der Seite „nicht angegeben“** (`GET /worktime`, `/worktime/edit`, Weiterleitung
  nach dem Löschen: der Filter fällt weg, nichts bricht), **beim PDF 400** „Ungültiger Kunde.“ (auch eine ID ohne Kontakt): Ein
  Zettel für alle darf nie entstehen, weil ein Kundenwert unlesbar war. Die Seite hat die Auswahl „Kunde“ (Alle; die Kunden mit
  Einträgen im Zeitraum bei dieser Person, alphabetisch ohne Rücksicht auf die Schreibweise, dazu der gewählte – eine unbekannte
  ID bleibt als „Kunde n (unbekannt)“ gewählt, der Filter fällt nie still auf „Alle“ –; „ohne Kunde“). Liste und Summe zeigen nur
  die passenden Einträge, die leeren Tage der Monatsansicht bleiben immer stehen. Der Filter bleibt über Speichern (verstecktes
  Feld `customer` – nicht zu verwechseln mit `customer_id`, dem Kunden des Eintrags), Löschen, Zeitraumwahl und PDF (`pdfBase`)
  erhalten; die Personenauswahl setzt ihn zurück; ein neuer Eintrag bekommt den gefilterten Kunden vorbelegt
  (`WT_DATA.filterCustomer`, `wtClear`). **Der Stundenzettel** (`generate_worktime_pdf(…, customer, with_empty_days)`) filtert vor
  allem anderen (Zeilen, Summe, Arbeitstage) und nennt den Kunden in einer vierten Kopfzeile bei y = 765 („Kunde: …“, bei „ohne
  Kunde“ so; mit „…“ auf `CUSTOMER_LINE_WIDTH` = 380 pt gekürzt, damit sie nicht ins Logo läuft – `customer_line`, höchstens die
  ersten 200 Zeichen), im rechten Unterschriftenblock (bei einem Kundenfilter dieser Kunde, auch ohne Arbeitseintrag im Zeitraum;
  ohne Filter wie bisher der eine Kunde der Einträge; bei „ohne Kunde“ keiner) und im Dateinamen
  (`Stundenzettel_<Person>_<Kunde>_<Zeitraum>.pdf`, „ohne Kunde“ = `ohne-Kunde`; je Name höchstens `FILE_PART_LENGTH` = 80
  Zeichen – ein Kontaktname hat keine Längengrenze). **„Mit leeren Tagen“** (Häkchen, beim Laden an;
  Adresse `empty`: nur `0`/`false`/`off` schalten aus, sonst an): Der Monatszettel listet dann auch die Tage ohne Eintrag, sonst
  nur die Tage mit Einträgen; über mehrere Monate ändert sich nichts. Ohne Filter bleibt jeder Zettel, wie er war
  (`tests/test_worktime_customer_filter.py`). **Die Notiz eines Eintrags hat höchstens `NOTE_MAX_LENGTH` = 300 Zeichen**
  (Entscheidung des Owners): Das Formularfeld trägt `maxlength` aus der Konstante; der Server lehnt beim Anlegen und Ändern (EINE
  Stelle: `handlers._wt_refusal`) eine längere Notiz ab – nichts gespeichert, 303 mit `?error=` und dem festen Satz „Die Notiz
  ist zu lang (höchstens 300 Zeichen).“, nie still gekürzt, ohne Teile der Notiz. **Gemessen und gespeichert wird ohne Leerraum
  am Rand** (`_wt_parse`: `strip()`) – sonst stünde hinter 300 Zeichen beliebig viel Leerraum in der Datenbank. Ebenso abgelehnt:
  eine **Art, die das Formular nicht kennt** („Bitte eine gültige Art wählen.“; sie gälte überall als „keine Arbeit“). Das Feld
  `customer_id` liest nur ASCII-Ziffern, alles andere heißt „kein Kunde“. Reihenfolge der Prüfungen: Uhrzeit, Art, Notiz,
  Überschneidung. Die Art aus der Datenbank steht auf der Seite nur maskiert. **Der Bestand bleibt:** Längere Notizen in der Datenbank werden weder
  gekürzt noch migriert (die Datenbankschicht begrenzt nichts), das PDF bricht sie wie bisher um und endet nach acht Zeilen mit
  „…“; wird so ein Eintrag geändert, greift die Prüfung (`tests/test_worktime_note_limit.py`)
