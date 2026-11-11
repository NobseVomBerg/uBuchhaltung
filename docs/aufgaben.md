# Offene Aufgaben

Kurzliste, öffentlich. Erledigtes wird gelöscht, nicht abgehakt – die Historie
steht in `git log`. Eine Zeile je Punkt; Details gehören in Commit oder Issue.

## Repo & Veröffentlichung
- awesome-selfhosted: frühestens ab 4.3.2027 (erster Release `v1.6.16` am 3.11.2026); Eintrag schreibt der Owner selbst (Listenregel: keine KI-generierten Beiträge)
- Docker-Demo (`-e UBUCHHALTUNG_DEMO=1`) einmal im echten Container prüfen und den Umzug einer 1.x-Installation auf 2.0.0

## Funktionen
- Sammelimport: Exportdateien und Kontoauszüge als Sammlung hineingeben, ohne auf Importreihenfolgen zu achten – Spalten frei gewählter Exporte erkennen und in einer Vorschau zuordnen, Quellsätze erst in eine Zwischenablage, ein gemeinsamer Abgleich mit sichtbaren Zweifelsfällen; braucht Schemaänderung und Design im Hauptthread (`/effort xhigh`)
- Kontoauszüge im MT940-Format (CAMT.053 und PDF: DKB, Volksbank Rottweil sind da)
- CAMT-Zusatzfelder (Valuta, EndToEndId, Mandatsreferenz, Gläubiger-ID) übernehmen – braucht Schemaänderung
- Rechnungen/Angebote mit gemischten Steuersätzen (7 % und 19 % je Position) – bisher ein Satz je Beleg
- Leistungszeitraum (von–bis) als Alternative zum Leistungsdatum, z. B. für monatliche Leistungen – PDF-Kopf und E-Rechnung (CII BillingSpecifiedPeriod / UBL InvoicePeriod); braucht Schemaänderung
- Umsatzsteuer-Voranmeldung (ELSTER)
- GoBD-Modus, je Datenverzeichnis einstellbar (Ersteinrichtung fragt: Steuerbüro oder selbst?); vor Umsetzung Design im Hauptthread (`/effort xhigh`):
  - Festschreibung abgeschlossener Zeiträume, Korrektur nur per Storno/Gegenbuchung, kein Löschen
  - Änderungsprotokoll (wer/wann/alt/neu), manipulationssicher; SQL-Konsole nur lesend oder aus
  - Gilt auch für Kasse und Fahrtenbuch; Abschalten nur protokolliert

## Kontenrahmen (SKR03/04/07 …)
Heute ist nur SKR04 ausgebaut, und zwar so weit, wie es der Owner braucht; die Auswahl
anderer Rahmen beim Anlegen eines Kontos ist bisher nur formal. Reihenfolge der Schritte:
1. **Funktionskonten statt fester Nummern** – Voraussetzung für alles Weitere; vor Umsetzung Design im Hauptthread (`/effort xhigh`).
   Rund 70 fest verdrahtete SKR04-Nummern in 12 Dateien (z. B. 3806, 1401/1406, 4400, Wartekonten 4405/4345/4340,
   Debitoren 10000). Stattdessen Rollen wie „Umsatzsteuer 19 %“, „Erlöse 7 %“, „Wartekonto“ je Kontenrahmen zuordnen
2. **Vollständige Kontenrahmen als Seed-Daten** (`seed_data/chart_of_accounts_<rahmen>.json`), zuerst SKR03;
   Quelle und Nutzungsrechte der Kontenlisten vorher klären
3. **Übersicht trotz vieler Konten:** Auswahllisten zeigen nur sichtbare Konten (`ShowInMenu` gibt es schon);
   sinnvolle Grundauswahl je Rahmen, alles andere bei Bedarf einschalten. Dazu Suche nach Nummer/Name in der Auswahl,
   Gruppierung nach Kontenklasse, zuletzt genutzte Konten oben; optional Branchenprofile als Vorauswahl
4. **Wahl des Kontenrahmens bei der Ersteinrichtung**
5. **Umstellung bestehender Datenbanken** (z. B. SKR03 → SKR04): braucht ein vollständiges Mapping, das nicht überall
   1:1 ist (Aufteilungen, Zusammenlegungen). Ablauf: Sicherung, Probelauf mit Bericht, nicht eindeutige Konten melden
   statt raten, dann Buchungen, Anlagen, Rechnungen und Steuerschlüssel umschlüsseln
- SKR07 (Österreich) erst, wenn die Steuerlogik dafür ansteht (andere Voranmeldung und Gewinnermittlung) – bewusst später

## Fehler & Technik
- DATEV-Export vor der ersten Weitergabe ans Steuerbüro gegen einen echten Buchungsstapel prüfen. Bekannt:
  - Forderung (10000/4405) und Umbuchung bei Zahlung (4405/4400) tragen beide einen Steuerschlüssel → je bezahlter Rechnung zweimal Umsatzsteuer
  - der BU-Schlüssel folgt der Geldrichtung statt der Art des Kontos (Erstattung auf ein Aufwandskonto bekommt einen Umsatzsteuer-Schlüssel)
  - Überträge zwischen zwei Geldkonten aus dem WISO-Import: Soll/Haben verkehrt – betrifft seit der Standard-Kasse 1600 auch Überträge Bank ↔ Kasse (Abhebung, Bareinzahlung)
  - Soll/Haben wird beim Export aus der aktuellen Kontenliste gelesen, das Vorzeichen stammt aus der beim Import – ändert sich die SKR-Nummer eines Bankkontos dazwischen, passt beides nicht mehr zusammen; besser beim Import festschreiben
- Split-Summe (`is_bank_effective`, `find_unbalanced_splits`) kennt das Verrechnungskonto 1460 nicht als Geldkonto: Eine Bankzeile mit dem einzigen Kind „X an 1460“ gilt bei Kasse ≠ 1460 als unausgeglichen (DATEV-Export gesperrt), bei Kasse = 1460 gilt „X an 1460“ plus „1460 an Bank“ als unausgeglichen – Regel je Spiegel/Zahlung neu fassen
- Dashboard zählt bei Bestandskasse = 1460 eigenständige WISO-Teile „X an 1460“ als Kasse und dazu die Bankzeile doppelt (die EÜR stimmt) – 1460 im Dashboard ausnehmen. Nicht die Kasse umstellen: Hängen schon Buchungen an 1460 oder 1600, deutet die Umstellung sie um (die Kontenverwaltung lehnt das ab)
- Dashboard zählt eine Umbuchung Anlage ↔ Erfolgskonto, der die Maske eine Kasse als Konto eingetragen hat, als Kassenbewegung (EÜR stimmt)
- Die Importe behandeln 1000–1099 als Kasse – das ist der SKR03-Bereich, in SKR04 liegt die Kasse auf 1600; die Richtung eines importierten Satzes beim Import festschreiben statt sie beim Lesen aus der aktuellen Kontenliste abzuleiten – erst dann lässt sich der Bereich gefahrlos ändern
- Eigenständige WISO-Kassensätze auf 1600 fehlen in EÜR/Dashboard, solange die Kasse der Installation nicht 1600 trägt (ältere Installationen: Kasse ohne SKR oder 1460). Die Kasse lässt sich nur auf 1600 stellen, solange noch keine Buchungen an 1600 hängen (vor dem ersten Import); danach bräuchte es eine Umstellung der gespeicherten Vorzeichen – nie als stille Migration
- Umbuchungen Privat ↔ Erfolgskonto ohne Bankbezug (z. B. Kfz-Privatnutzung „2100 an 4645“, privat bezahlte Betriebsausgabe „6815 an 2180“, Entnahmewert eines Anlageguts) fehlen in der EÜR – fachlich mit dem Steuerbüro klären
- Alte AfA-Sätze (vor 2.0.5: Aufwandskonto auf einem Bank-/Kassenkonto, kein Gegenkonto) stehen im DATEV-Export ohne Gegenkonto; keine automatische Reparatur – Buchung löschen (setzt das Jahr zurück) und neu buchen
- Vorzeichen der WISO-AfA-Sätze (z. B. „6222 an 0520“) an echten Daten prüfen: gespeichert ist der Rohbetrag, die EÜR liest positiv als „Konto im Soll“
- Anlage wieder auf „aktiv“ setzen leert das Abgangsdatum nicht (AfA der Folgejahre bleibt gesperrt); Status „verschrottet“ ohne Datum sperrt nicht
- WISO-Import nach einer in der App gebuchten AfA desselben Jahres zählt das Jahr doppelt (nur bei paralleler Nutzung beider Programme)
- AfA-Buchung nimmt als Aufwandskonto jedes Erfolgskonto 4000–7999 an, also auch SKR04-Erlöskonten (bewusst weit, weil selbst angelegte SKR03-Konten wie 4830 dort liegen) – mit den Funktionskonten enger fassen
- AfA im Abgangsjahr wird voll statt zeitanteilig geplant (`calculate_depreciation_plan` kennt das Abgangsdatum nicht)
- Der Auto-Abgleich kann eine AfA-Buchung (Umbuchung, 31.12.) an eine gleich hohe Bankbewegung vom 31.12. hängen
- Dashboard zählt einen Übertrag Giro → Kasse als Einnahme und Ausgabe
- Bankbewegung und ihre Buchungssätze in einer Transaktion speichern – heute nur „erst prüfen, dann schreiben“; ein Datenbankfehler beim Schreiben hinterlässt einen halben Stand
- Ein eigenständiger Buchungssatz bekommt beim Speichern in der Maske das vorgewählte Bankkonto und gilt danach im Kontoauszug-Import als vorhandene Bankbewegung
- Kontoauszug-Import erkennt Duplikate nur über Konto + Datum + Betrag (je Datei gezählt, über Dateien das Maximum): Bankreferenz (AcctSvcrRef/EndToEndId) speichern, gebündelt mit den CAMT-Zusatzfeldern (Schemaänderung). Bis dahin: CAMT-Sammelbuchung gegen PDF-Summenzeile wird doppelt angelegt; zwei verschiedene Auszüge desselben Tags mit je einem echten, gleichen Umsatz verlieren einen
- Vorschau des Kontoauszug-Imports nach einem Einzelimport neu berechnen – die übrigen Karten zeigen sonst veraltete „neu“-Zahlen (das Ergebnis des Imports stimmt)
- Kontoauszug-Import: Umsätze, die „Alle importieren“ bzw. der Einzelimport als Duplikat aus einer anderen Datei verwirft, lassen sich nicht gezielt doch importieren (z. B. zwei echte gleiche Beträge am selben Tag in zwei mitten am Tag geteilten Auszügen). Die Vorschau markiert abweichende Texte („Text weicht ab, bitte prüfen“), nachtragen geht nur von Hand – es fehlt eine Option „trotzdem importieren“. Dazu: `skipped_upload` ist beim Einzelimport immer 0 (jeder Aufruf liest den Bestand neu), die Meldung nennt dort also nie „aus anderer Datei dieses Uploads“
- WISO-Datenbank-Import übernimmt CSV-Buchungen nur bei gleichem Beleg-Nr./Datum/Konto/Betrag **und Text** (das Gegenkonto darf nicht widersprechen); eine CSV-Zeile mit nachträglich geändertem Text, Gegenkonto oder Betrag wird nicht wiedererkannt – die WISO-Satz-Id als eigene Spalte würde das exakt machen (Schemaänderung, mit der Bankreferenz bündeln)
- Nicht wiedererkannte CSV-Zeilen bleiben als zweite Buchung stehen; auf Kasse/1460 und bei Umbuchungen Anlage ↔ Erfolgskonto zählen sie doppelt in der EÜR, bis eine gelöscht ist (Liste „möglicherweise doppelt“ auf der Seite Verschiedenes)
- Der Weg „erst CSV, dann Datenbank“ ist nur mit synthetischen Daten geprüft – Gegentest mit einem echten CSV-/Datenbank-Paar steht aus (Texte, Umschlüsselung von Unterkonten, Vorzeichen von Gutschriften)
- Der Datenbankweg liest Texte als latin-1 statt cp1252 (Anzeige von „€“ u. ä.; für den Vergleich mit der CSV umgesetzt, gespeichert wird der gelesene Text); „…“ (0x85) geht schon im Leser verloren (`_clean` fasst es als Leerraum zusammen) – solche Texte werden nicht wiedererkannt
- Neu angelegte Buchungen der WISO-Importe stehen nicht im SQL-Protokoll (nur die Übernahmen per UPDATE)
- Rechnungsformular (`/invoice?id=…`) läuft bei 1440 px Breite rechts aus dem Viewport
- Invoices-Zeilen werden per Spaltenindex gelesen (u. a. `server/pages_invoice.py`) – bricht bei Schemaänderungen, besser Spaltennamen
- CI testet nur Python 3.13, README nennt 3.10+ → Matrix ergänzen oder Angabe anpassen
- KI-Doku entschlacken: `CLAUDE.md` und `AGENTS.md` auf Doppelungen prüfen
- Einheiten auf Artikel-Listen/-Stammdaten prüfen (lesbar statt UN/ECE-Code)
- Pfadprüfung beim Ausliefern gespeicherter Rechnungen (`stored_invoice_document`) – Security-Review, niedrige Priorität
- Rechnung speichern scheiterte einmal im Docker-Mehrbenutzerbetrieb mit „FOREIGN KEY constraint failed“ (DB danach sauber, gleicher Request lokal ok, nicht mehr reproduzierbar) – ab 2.0.1 nennt die Meldung den Verweis; bei Wiederauftreten Meldung und `docker logs` auswerten
- Angebot speichern wie Rechnungen in einer Transaktion (Kopf + Positionen) – heute einzeln committet, ein Fehler hinterlässt halbe Belege
- PDF-Kopf ans Formular angleichen: „Rechnungsdatum“, Leistungsdatum direkt darunter – Wertspalte für das längere Label verschieben, Golden-Dateien neu erzeugen
