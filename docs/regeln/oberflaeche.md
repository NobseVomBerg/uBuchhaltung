# Oberfläche: Layout und Zeitraum-Menüzeile

## Styling

- **Rechnungen und Angebote (seit 2.5.1):** Das Belegformular rechts ist fest 820px breit (es bildet das Blatt nach); das Raster
  trägt diese Breite (`.gridDocForm` neben `.grid2Cols`: `minmax(0, 1fr) 820px`), nie ein Inline-`min-width` – in der
  Drittelspalte von `.grid2Cols` ragte das Formular über den rechten Rand. Die Liste links (`.docList`) macht jede Spalte so
  schmal wie ihren Inhalt, der Kunde bekommt den Rest (gekürzt, ganzer Name im Tooltip); von 1441 bis 1700px Fensterbreite
  entfallen Netto und „gültig bis“ (`.docWide`), bis 1440px steht das Formular unter der niedrig gehaltenen Liste. Gerechnet
  wird in CSS-Pixeln: Ein 1920er Bildschirm mit 150 % Skalierung zählt als 1280px – die Seite nie selbst gegen die
  Skalierung verkleinern (`zoom`, `devicePixelRatio`). `tests/test_document_page_layout.py`
- **Dritte Menüzeile (Zeitraum, seit 2.6.5):** Sie entsteht auf jeder Seite mit Zeitraum (Dashboard, Buchungen, Rechnungen, Belege, Arbeitszeiten,
  Fahrten und die vier Auswertungen) allein aus `period.period_filter_widget` – Von/Bis, fünf Jahresknöpfe in einem eigenen Block, Monatsknöpfe –
  und **nie als eigenes Formular einer Seite**: Auswahlfelder und farbige Knöpfe sind höher als die Knöpfe der Zeile, die Querlinie unter den drei
  Menüzeilen spränge von Seite zu Seite (gemessen: 113 statt 109px, solange BWA, EÜR, Summen- und Saldenliste und Liquidität ein eigenes Formular
  hatten). Je Seite hält ein Test fest, dass der Inhalt von `.header3` genau das ist, was die Hilfsfunktion liefert (`tests/test_period_menu_row.py`).
  Die Knöpfe der Zeile sind etwas schmaler als der Browser-Standard (`.header3 button` in `buch.css`): So bleiben fünf Jahres- und zwölf Monatsknöpfe bis
  hinunter zu rund 1040px Fensterbreite in einer Zeile (gemessen: die Linie liegt von 1500 bis 1040px auf allen Seiten bei 109px; darunter bricht der
  Monatsblock um). Der aktive Jahres- und Monatsknopf ist in beiden Farbschemata hervorgehoben (`button.active`).
  Es gibt drei Arten von Monatsknöpfen (`months=`): `toggle` (Standard: ein Klick auf den aktiven Monat schaltet aufs ganze Jahr), `always`
  (Monatsauswertungen: immer genau ein Monat ist aktiv, sein Knopf führt auf ihn selbst) und `none` (Dashboard: kein Monatsblock, die Jahresknöpfe
  wählen immer das ganze Jahr). Zusatzparameter einer Seite stehen in `extra_params` (eine Liste wiederholt den Schlüssel: `acct=1&acct=2`).
  **Die Auswertungen nehmen den gemeinsamen Zeitraum** (Adresse > Cookie > laufendes Jahr, `resolve_period`, das Cookie wie überall); die
  Monatsauswertungen (BWA, Summen- und Saldenliste, Liquidität) leiten ihren Monat daraus ab (`period.period_month`): Ein Zeitraum, der genau ein
  Kalendermonat ist, zeigt diesen Monat, jeder andere den Monat des Bis-Datums – ein ganzes Jahr also seinen Dezember –, im laufenden Jahr höchstens
  den laufenden Monat. Eine Zusatzauswahl einer Seite (die „Darstellung“ von BWA und Liquidität: ein kleines GET-Formular mit den versteckten Feldern
  `from`/`to`) steht im Inhalt, in der Zeile mit dem PDF-Knopf – nie in der Menüzeile
