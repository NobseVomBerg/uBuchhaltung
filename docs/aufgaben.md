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
- Zahlungssätze einer Zuordnung (Marke `invoice_payment`) im Buchungssatz-Editor schreibgeschützt zeigen – bisher lassen sie sich dort ändern, und das Löschen einer Zeile entfernt die ganze Zuordnung
- Skonto bei der Zahlungszuordnung (Abzug als eigener Satz, Steuerkorrektur)
- Rechnung berichtigen mit Fassungen und Protokoll: Die bisherige Fassung (PDF/XML) bleibt als Datei erhalten, jede Änderung an Begleittext, Anschrift, Bestellnummer, Leitweg-ID und Leistungsdatum einer versendeten Rechnung wird protokolliert (wer/wann/alt/neu), die berichtigte Fassung trägt einen Hinweis (CII BT-22 / UBL Note) und muss dem Kunden erneut zugehen; braucht Schemaänderung. Bis dahin sind diese Felder direkt änderbar, ohne Spur
- Stornobeleg: Stornorechnung als eigener Beleg mit eigener Nummer (CII Typ 381 mit Bezug auf die Ursprungsrechnung, UBL CreditNote, PDF „Stornorechnung“), Gegenbuchung der Forderung statt Löschen, Zahlungen auf die neue Rechnung verschieben oder Erstattung; optional „Stornieren und neu ausstellen“. Ersetzt den manuellen Status „storniert“ (heute ohne Beleg und ohne Gegenbuchung); braucht Schemaänderung
- Gutschrift und Erstattung als negative Zuordnung – erst zusammen mit einem Stornobeleg; bis dahin lassen sich nur Zahlungseingänge zuordnen
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
- DATEV-Export vor der ersten Weitergabe ans Steuerbüro gegen einen echten Buchungsstapel prüfen (inkl. der BU-Schlüssel für 16 %/5 %). Bekannt:
  - Forderung (10000/4405) und Umbuchung bei Zahlung (4405/4400) tragen beide einen Steuerschlüssel → je bezahlter Rechnung zweimal Umsatzsteuer
  - der BU-Schlüssel sollte der Art des Zweckkontos folgen (Erlös ↔ Aufwand) statt der Geldrichtung – eine Erstattung auf ein Aufwandskonto bekommt heute einen Umsatzsteuer-Schlüssel; das Zweckkonto steht mit der Form des Satzes (`Bookings.EntryForm`) fest, damit lässt sich das sauber lösen
- Split-Summe (`is_bank_effective`, `find_unbalanced_splits`) kennt das Verrechnungskonto 1460 nur im Spiegel mit bekannter Lesart (`mirror_coa`/`mirror_counter`, Position zum Konto der Bankbewegung); bei einer Zahlung über 1460 und bei Spiegeln des Bestands (`mirror_legacy`) zählt sie weiter nur die eigenen Bankkonten: Eine Bankzeile mit dem einzigen Kind „X an 1460“ gilt bei Kasse ≠ 1460 als unausgeglichen (DATEV-Export gesperrt), bei Kasse = 1460 gilt „X an 1460“ plus „1460 an Bank“ als unausgeglichen – Regel je Zahlung neu fassen
- Dashboard zählt bei Bestandskasse = 1460 eigenständige WISO-Teile „X an 1460“ als Kasse und dazu die Bankzeile doppelt (die EÜR stimmt) – 1460 im Dashboard ausnehmen. Nicht die Kasse umstellen: Hängen schon Buchungen an 1460 oder 1600, deutet die Umstellung sie um (die Kontenverwaltung lehnt das ab)
- Dashboard zählt eine Umbuchung Anlage ↔ Erfolgskonto, der die Maske eine Kasse als Konto eingetragen hat, als Kassenbewegung (EÜR stimmt)
- Eigenständige WISO-Kassensätze auf 1600 fehlen in EÜR/Dashboard, solange die Kasse der Installation nicht 1600 trägt (ältere Installationen: Kasse ohne SKR oder 1460). Die Kasse lässt sich nur auf 1600 stellen, solange noch keine Buchungen an 1600 hängen (vor dem ersten Import); danach bräuchte es eine Umstellung der gespeicherten Form und Vorzeichen (mit Probelauf und Sicherung) – nie als stille Migration
- Prüfliste für Spiegel und Umbuchungen des Bestands (Seite *Verschiedenes*, nur lesend, mit Probelauf): Spiegel mit unbekannter Lesart (`mirror_legacy` – die Importe zählen die, die sie wiedererkennen, als `legacy_mirrors`, ändern sie aber nicht) und Sätze ohne Geldseite, deren Konto inzwischen ein Geldkonto ist, mit Vorschlag und Wirkung (Soll/Haben im Export, Dashboard, Split-Summe) – sicher / wahrscheinlich / unklar; Übernehmen nur nach Bestätigung (wahrscheinliche einzeln), vorher Sicherung, jedes UPDATE im Replay-Log, „unklar“ wird nie geändert
- Vorzeichen negativer Bruttobeträge (Storno, Gutschrift) beim WISO-Import an einer echten `DB1.FDB` messen (Zeilen mit `AMOUNTGROSS < 0`, nach Kontoklasse und Rechnungs-Id), dann festschreiben: Heute bestimmt bei genau einer liquiden Seite die Geldrichtung das Vorzeichen, und der Import meldet jeden solchen Satz mit negativem Brutto (`negative_gross`) – gleich, ob die Zahl dabei ihr Vorzeichen ändert (ein Storno „6815 an 1800“ bleibt −119 und wird als Ausgabe gelesen); ohne liquide Seite oder mit zwei bleibt der Rohbetrag
- Ein Importsatz ohne liquide Seite und mit leerem oder unbekanntem Gegenkonto (Form `transfer`, z. B. „6815 an –“) steht unter einer Bankbewegung in der EÜR ohne Vorsteuer: Die EÜR liest ihn als Umbuchung (Konto −netto), ein Zweckkonto für die Vorsteuer 1406 gibt es nicht
- Die Übernahme CSV → Datenbank erkennt eine Zeile auf Konten 1000–1099 aus 2.0.x (mit dem Betrag der alten Regel gespeichert) nur, wenn ihr Konto im Kontenrahmen stand (`COA_ID` gesetzt); fehlte es, entsteht eine zweite Buchung. Der Re-Import des Textexports erkennt auch diese Zeilen
- Umbuchungen Privat ↔ Erfolgskonto ohne Bankbezug (z. B. Kfz-Privatnutzung „2100 an 4645“, privat bezahlte Betriebsausgabe „6815 an 2180“, Entnahmewert eines Anlageguts) fehlen in der EÜR – fachlich mit dem Steuerbüro klären
- Alte AfA-Sätze (vor 2.0.5: Aufwandskonto auf einem Bank-/Kassenkonto, kein Gegenkonto) stehen im DATEV-Export ohne Gegenkonto; keine automatische Reparatur – Buchung löschen (setzt das Jahr zurück) und neu buchen
- Vorzeichen der WISO-AfA-Sätze (z. B. „6222 an 0520“) an echten Daten prüfen: gespeichert ist der Rohbetrag mit der Form `transfer`, die EÜR liest positiv als „Konto im Soll“
- Anlage wieder auf „aktiv“ setzen leert das Abgangsdatum nicht (AfA der Folgejahre bleibt gesperrt); Status „verschrottet“ ohne Datum sperrt nicht
- WISO-Import nach einer in der App gebuchten AfA desselben Jahres zählt das Jahr doppelt (nur bei paralleler Nutzung beider Programme)
- AfA-Buchung nimmt als Aufwandskonto jedes Erfolgskonto 4000–7999 an, also auch SKR04-Erlöskonten (bewusst weit, weil selbst angelegte SKR03-Konten wie 4830 dort liegen) – mit den Funktionskonten enger fassen
- AfA im Abgangsjahr wird voll statt zeitanteilig geplant (`calculate_depreciation_plan` kennt das Abgangsdatum nicht)
- Der Auto-Abgleich kann eine AfA-Buchung (Umbuchung, 31.12.) an eine gleich hohe Bankbewegung vom 31.12. hängen
- Dashboard zählt einen Übertrag Giro ↔ Kasse als Einnahme bzw. Ausgabe der Kasse (Abhebung +, Bareinzahlung −, aus der Form des Spiegels gelesen) – ein Übertrag gehört nicht in Einnahmen und Ausgaben, sondern als Umbuchung herausgerechnet
- Bankbewegung und ihre Buchungssätze in einer Transaktion speichern – heute nur „erst prüfen, dann schreiben“; ein Datenbankfehler beim Schreiben hinterlässt einen halben Stand
- Bank-Editor: eine von Hand angelegte, später zugeordnete Buchung wird beim Entfernen der Zeile gelöscht (nicht von im Editor angelegten Zeilen unterscheidbar). Dasselbe gilt für Sätze aus dem WISO-Textexport (keine Quellklammer)
- Bank-Editor: Steuer einer zugeordneten Buchung lässt sich erst beim zweiten Speichern leeren
- Bank-Editor: Zuordnen eines Satzes mit leerem Konto vorne und Zweckkonto hinten ersetzt das Zweckkonto durch das Konto der Bank – stattdessen die leere Seite füllen oder den Satz unverändert lassen
- Bank-Editor: Das Gegenkonto eines zugeordneten Satzes mit Zweckkonto vorne lässt sich in der Zeile nicht ändern (nur über die Buchungsmaske)
- Bankmaske übernimmt den Kontakt des ersten Satzes auf eine Bankbewegung ohne Kontakt
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
- Rechnung speichern scheiterte einmal im Docker-Mehrbenutzerbetrieb mit „FOREIGN KEY constraint failed“ (DB danach sauber, gleicher Request lokal ok, nicht mehr reproduzierbar) – ab 2.0.1 nennt die Meldung den Verweis; bei Wiederauftreten Meldung und `docker logs` auswerten
- PDF-Kopf ans Formular angleichen: „Rechnungsdatum“, Leistungsdatum direkt darunter – Wertspalte für das längere Label verschieben, Golden-Dateien neu erzeugen
