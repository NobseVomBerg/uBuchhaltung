# Arbeitszeiten, Fahrtenbuch, Stundenzettel

## Arbeitszeit

- **Rechnet in Minuten** (`server/pages_worktime.py`: `clock_minutes`, `compute_minutes`, `crosses_midnight`, `format_hours`). Uhrzeiten nur `HH:MM` aus ASCII-Ziffern (0–23, 0–59), sonst 0 Minuten und beim Speichern abgelehnt („Bitte die Uhrzeit als HH:MM angeben.“).
- **Ende vor Beginn = Folgetag** (+ 24 h; Ende = Beginn: 0); Eintrag zählt ganz zu seinem Datum, „(+1)“ in Liste und PDF.
- **Summen aus Minuten, nie aus gerundeten Stunden** (`compute_hours` nur Zeilenanzeige). „Arbeitstage“ = Kalendertage mit Arbeit > 0 Minuten, auf Seite und Stundenzettel gleich.
- Überlappung `_wt_has_overlap` rechnet auf der Zeitachse des Eintragsdatums (über Mitternacht über 24:00 hinaus), Nachbartage nicht. Vorschau `wtUpdateHours` rechnet dieselbe Regel.

## Fahrtenbuch

- **Bleibt locker** (Entscheidung des Owners): einzelne Fahrten, ohne Tachostand, beliebige Reihenfolge. **Beim Speichern nie ein Anschluss an die Vorfahrt prüfen, weder als Fehler noch Hinweis – nicht einführen.**
- Abgelehnt nur Unmögliches, an EINER Stelle (`handlers._trip_fields`, rein, Anlegen und Ändern): Fahrer/Start-Datum/Ziel fehlen, Datum kein ISO-Datum, Ende vor Start, Uhrzeit nicht `HH:MM`, Kilometer keine ganze Zahl 0 bis 9 999 999,
  Tacho Ende unter Tacho Start, ungültiger Beleg, Text über 500 Zeichen. Meldung als `?error=` (`script_json` + `appMsg`), nichts gespeichert; Formular prüft vorab (`trValidate`), Server maßgeblich.
  `insert_trip`/`update_trip` melden Fehlschlag (`None`/`False`).
- **Lückenprüfung nur auf Wunsch** (GET `/trips?…&check_vehicle=&check_year=`, rein lesend): `db.trips.logbook_gaps` (rein), alle Fahrten eines Fahrzeugs im Kalenderjahr, alle Fahrer, Fahrzeug nach `strip()` + `casefold()`.
  Lücke = Fahrt mit Tacho Start, deren nächster Vorgänger mit Tacho Ende anderen Stand hat; Fahrten ohne Tachostand dazwischen zählen (`between`); nach Start ohne Ende ist der Stand unbekannt. Höchstens 100 Einträge plus Zahl.
- Fahrzeuge sind Freitext (Stammliste wäre Schemaänderung).

## „Als neu kopieren“ (Arbeitszeiten, Fahrten)

- **Speichert nichts** (`type="button"`, `wtCopyAsNew`/`trCopyAsNew`): leert die ID, „neu“, Datum heute; angelegt erst mit „Speichern“ (`/worktime/add`, `/trips/add`, alle Prüfungen).
  Arbeitszeit: Vorlage des letzten Eintrags füllt eine Kopie nicht auf (`wtIsCopy`). Fahrt: Tachostände und Beleg bleiben leer. `tests/test_copy_as_new.py`

## Stundenzettel-PDF (`export/pdf_worktime.py`)

- `page_layout(row_count)`: keine Datenzeile/Textzeile unter `BOTTOM_Y`, Summe und Unterschriften zusammen auf der letzten Seite (notfalls eigene Seite), kein Eintrag über zwei Seiten, „Seite n von m“ nur bei mehreren Seiten.
- **Info-Spalte:** Art des Tages, auf Wunsch Arbeitsort (`with_location`), Pausenzeiten, auf Wunsch Notiz (`with_notes`), mit „ · “. Häkchen „Mit Notiz“/„Mit Arbeitsort“ **beide beim Laden an**; Adresse nennt immer beide (`notes`, `location`: nur `1`/`true`/`on` schalten ein, ohne Angabe keines).
- Text **bricht um** (`info_lines`) und endet nach `MAX_INFO_LINES` = 8 Zeilen mit „…“ – nie still abgeschnitten. Gemessen **wie gedruckt** (`printed_text`: WinAnsi) mit Breiten der eingebetteten Schrift (`info_width`).
  **Laufzeit hängt nur von den gedruckten Zeilen ab** (jedes Zeichen höchstens zweimal, nach neun Zeilen Ende); Notiz, Pausentext, Art sind unbegrenzt – nie je Teilstück den ganzen Rest messen. `tests/test_worktime_pdf_info.py`

## Kundenfilter und leere Tage

- **EINE Stelle** (`server/pages_worktime.py`): `customer_filter` (streng: `''` = alle, `'none'` = ohne Kunde, sonst positive ASCII-Ganzzahl 64 Bit, alles andere `None` – auch `customer=%20`), `entry_matches_customer`, `customer_query`; Seite und PDF nehmen dieselbe.
- Kundenzettel = Einträge des Kunden **plus Einträge einer anderen Art als „Arbeit“ ohne Kunden** (Urlaub, Krank, Feiertag – nur Arten des Formulars, `KIND_LABELS`). Nie: Arbeit anderer Kunden, Arbeit ohne Kunde, Urlaub eines anderen Kunden. „ohne Kunde“ = alle ohne Kunden; kein Filter = alles.
- **Ungültig:** Seite = „nicht angegeben“ (`GET /worktime`, `/worktime/edit`, Weiterleitung nach Löschen); **PDF 400** „Ungültiger Kunde.“ (auch ID ohne Kontakt) – nie ein Zettel für alle wegen unlesbarem Kundenwert.
- Auswahl „Kunde“: unbekannte ID bleibt als „Kunde n (unbekannt)“ gewählt, der Filter fällt nie still auf „Alle“. Liste/Summe nur passende Einträge, leere Tage der Monatsansicht bleiben.
  Filter bleibt über Speichern (verstecktes Feld `customer`, nicht `customer_id`), Löschen, Zeitraumwahl, PDF (`pdfBase`); Personenauswahl setzt ihn zurück; neuer Eintrag mit gefiltertem Kunden vorbelegt (`WT_DATA.filterCustomer`, `wtClear`).
- **Stundenzettel** (`generate_worktime_pdf(…, customer, with_empty_days)`): filtert vor allem anderen (Zeilen, Summe, Arbeitstage); vierte Kopfzeile y = 765 („Kunde: …“, gekürzt auf `CUSTOMER_LINE_WIDTH` = 380 pt, `customer_line`, höchstens 200 Zeichen);
  Unterschriftenblock: bei Kundenfilter dieser Kunde (auch ohne Arbeitseintrag), ohne Filter der eine Kunde der Einträge, bei „ohne Kunde“ keiner; Dateiname `Stundenzettel_<Person>_<Kunde>_<Zeitraum>.pdf` („ohne Kunde“ = `ohne-Kunde`, je Name höchstens `FILE_PART_LENGTH` = 80).
- **„Mit leeren Tagen“** (beim Laden an; `empty`: nur `0`/`false`/`off` schalten aus): Monatszettel listet auch Tage ohne Eintrag; über mehrere Monate unverändert. Ohne Filter bleibt jeder Zettel unverändert. `tests/test_worktime_customer_filter.py`

## Notiz und Art

- **Notiz höchstens `NOTE_MAX_LENGTH` = 300 Zeichen** (Entscheidung des Owners): Formularfeld `maxlength` aus der Konstante; Server lehnt beim Anlegen und Ändern ab (EINE Stelle `handlers._wt_refusal`): nichts gespeichert, 303 mit `?error=` und
  „Die Notiz ist zu lang (höchstens 300 Zeichen).“, nie still gekürzt. **Gemessen und gespeichert ohne Leerraum am Rand** (`_wt_parse`: `strip()`).
- Abgelehnt auch eine **Art, die das Formular nicht kennt** („Bitte eine gültige Art wählen.“). `customer_id` liest nur ASCII-Ziffern, sonst „kein Kunde“. Prüfreihenfolge: Uhrzeit, Art, Notiz, Überschneidung. Die Art steht nur maskiert auf der Seite.
- **Bestand bleibt:** längere Notizen weder gekürzt noch migriert; PDF bricht sie um (8 Zeilen, „…“); beim Ändern greift die Prüfung. `tests/test_worktime_note_limit.py`
