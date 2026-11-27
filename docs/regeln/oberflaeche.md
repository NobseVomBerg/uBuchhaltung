# Oberfläche: Layout und Zeitraum-Menüzeile

## Styling

- **Für den PC-Bildschirm:** uBuchhaltung ist eine Webanwendung für den Schreibtisch – kompakte Dichte, keine Handy- oder Touch-Optik (große Knöpfe, hohe Zeilen). Eine Handy-App wäre ein eigenes Vorhaben.
- **Rechnungen und Angebote:** Das Belegformular rechts ist fest 820px breit (bildet das Blatt nach); das Raster trägt die Breite (`.gridDocForm` neben `.grid2Cols`: `minmax(0, 1fr) 820px`), **nie ein Inline-`min-width`**.
  Liste links (`.docList`): jede Spalte so schmal wie der Inhalt, Kunde bekommt den Rest (gekürzt, ganzer Name im Tooltip); 1441–1700px: Netto und „gültig bis“ entfallen (`.docWide`); bis 1440px steht das Formular unter der niedrigen Liste.
  Gerechnet wird in CSS-Pixeln – die Seite nie selbst gegen die Skalierung verkleinern (`zoom`, `devicePixelRatio`). `tests/test_document_page_layout.py`
- **Dritte Menüzeile (Zeitraum):** Auf jeder Seite mit Zeitraum (Dashboard, Buchungen, Rechnungen, Belege, Arbeitszeiten, Fahrten, die fünf Auswertungen) entsteht sie allein aus `period.period_filter_widget`
  (Von/Bis, fünf Jahresknöpfe in eigenem Block, Monatsknöpfe) und **nie als eigenes Formular einer Seite** (sonst springt die Querlinie unter den drei Menüzeilen von Seite zu Seite).
  Je Seite hält ein Test fest, dass der Inhalt von `.header3` genau das Ergebnis der Hilfsfunktion ist (`tests/test_period_menu_row.py`, für die Voranmeldung `tests/test_ustva.py`).
  - Die Knöpfe der Zeile sind schmaler als der Browser-Standard (`.header3 button` in `buch.css`): fünf Jahres- und zwölf Monatsknöpfe bleiben bis ca. 1040px Fensterbreite in einer Zeile. Aktiver Jahres-/Monatsknopf in beiden Farbschemata hervorgehoben (`button.active`).
  - Monatsknöpfe (`months=`): `toggle` (Standard: Klick auf den aktiven Monat schaltet aufs ganze Jahr), `always` (Monatsauswertungen: immer genau ein Monat aktiv), `none` (Dashboard: kein Monatsblock, Jahresknöpfe wählen das ganze Jahr).
    Zusatzparameter in `extra_params` (Liste wiederholt den Schlüssel: `acct=1&acct=2`).
  - **Auswertungen nehmen den gemeinsamen Zeitraum** (Adresse > Cookie > laufendes Jahr, `resolve_period`); die Monatsauswertungen (BWA, Summen- und Saldenliste, Liquidität, Umsatzsteuer-Voranmeldung) leiten den Monat daraus ab (`period.period_month`):
    genau ein Kalendermonat → dieser; sonst der Monat des Bis-Datums (ganzes Jahr = Dezember), im laufenden Jahr höchstens der laufende Monat.
  - Eine Zusatzauswahl einer Seite (z. B. „Darstellung“ von BWA und Liquidität oder „Anmeldezeitraum“ (`art`) der Umsatzsteuer-Voranmeldung: kleines GET-Formular mit versteckten Feldern `from`/`to`) steht im Inhalt in der Zeile mit dem PDF-Knopf – nie in der Menüzeile. Die Voranmeldung ist eine Monatsauswertung mit Monatsknöpfen `always`; `art` steht in den Adressen der Knöpfe, solange es nicht `monat` ist.
