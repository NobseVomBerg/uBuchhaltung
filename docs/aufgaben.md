# Offene Aufgaben

Kurzliste, öffentlich. Erledigtes wird gelöscht, nicht abgehakt – die Historie
steht in `git log`. Eine Zeile je Punkt; Details gehören in Commit oder Issue.

## Repo & Veröffentlichung
- Docker-Demo (`-e UBUCHHALTUNG_DEMO=1`) einmal im echten Container prüfen und den Umzug einer 1.x-Installation auf 2.0.0

## Funktionen
- Sammelimport: Exportdateien und Kontoauszüge als Sammlung hineingeben, ohne auf Importreihenfolgen zu achten – Spalten frei gewählter Exporte erkennen und in einer Vorschau zuordnen, Quellsätze erst in eine Zwischenablage, ein gemeinsamer Abgleich mit sichtbaren Zweifelsfällen; braucht Schemaänderung und vorher einen Entwurf
- Kontoauszüge im MT940-Format (CAMT.053, PDF: DKB und Volksbank Rottweil und CSV: DKB sind da)
- Kontoauszug als CSV anderer Banken (heute liest `importers/dkb_csv.py` nur die Umsatzliste der DKB)
- CAMT-Zusatzfelder (Valuta, EndToEndId, Mandatsreferenz, Gläubiger-ID) übernehmen – braucht Schemaänderung
- Rechnungen mit mehreren Steuersätzen oder mit einem Erlöskonto aus den Stammdaten „Steuersätze“: Der Umzug der Zahlungen auf eine Ersatzrechnung (nur ein Hinweis, sie bleiben an der stornierten Rechnung) und „Zuordnung neu aufbauen“ (Gründe `mixed`, `tax_entry`: nur der offene Betrag wird neu gerechnet) lassen solche Rechnungen aus – je Satz bzw. mit dem Konto des Eintrags neu zu buchen, mit denselben Bedingungen wie bei einem Satz, nicht gelockert
- Steuersätze aus den Stammdaten (seit 2.8.0), offen: ein Wartekonto für weitere Sätze (ein Beleg mit z. B. 5,5 % bucht heute keine Forderung, sondern erst mit der Zahlung – dafür braucht es eine feste Kontonummer je Satz, keine frei wählbare); BU-Schlüssel für weitere Sätze und für steuerfreie Umsätze im DATEV-Export; Befreiungscode der E-Rechnung (BT-121) neben dem Befreiungsgrund; Einkaufs- und Wareneingangskonten zum jährlich wechselnden Durchschnittssatz nach § 24 UStG legt der Nutzer selbst an; eigene Steuerkonten für 5 % und 16 % (zweites Halbjahr 2020) – die Summen- und Saldenliste führt sie auf den Konten von 7 % und 19 %
- WISO-Datenbank-Import: Rechnungen mit mehreren Steuersätzen je Position übernehmen (Kennzeichen setzen, Summen und Buchungen je Satz) – bisher trägt die importierte Rechnung einen Satz im Kopf, und die Prüfliste nennt sie bei verschiedenen Sätzen der Positionen als `unflagged_rate_mix`
- Leistungszeitraum (von–bis) als Alternative zum Leistungsdatum, z. B. für monatliche Leistungen – PDF-Kopf und E-Rechnung (CII BillingSpecifiedPeriod / UBL InvoicePeriod); braucht Schemaänderung
- Zahlungssätze einer Zuordnung (Marke `invoice_payment`) im Buchungssatz-Editor schreibgeschützt zeigen – bisher lassen sie sich dort ändern, und das Löschen einer Zeile entfernt die ganze Zuordnung
- Skonto bei der Zahlungszuordnung (Abzug als eigener Satz, Steuerkorrektur)
- Zahlungsvorschläge (seit 2.8.1), offen: Skonto und Teilbeträge im Vorschlag berücksichtigen, „alle eindeutigen zuordnen“ in einem Schritt, ein Abzeichen an der Zeile der Buchungsliste, die Wahl des Erlöskontos direkt im Kasten
- Zuordnung neu aufbauen, Schritt 2: markierte Zahlungssätze ohne Zuordnung (wiederherstellen oder entfernen, Wahl im Probelauf) und Sätze „Bank an Debitoren“ ohne Forderung
- Steuerfälle bei Rechnungen und Angeboten: Reverse Charge, innergemeinschaftliche Lieferung und Ausfuhr (eigene Kategorien der E-Rechnung, die USt-IdNr. des Käufers als Pflicht bei Reverse Charge und innergemeinschaftlicher Lieferung, eigene Erlöskonten) – steuerfreie Leistungen nach § 4 gibt es seit 2.8.0 über die Stammdaten „Steuersätze“; 0 % ohne Eintrag der Stammdaten und ohne § 19 ergibt weiter die Kategorie Z ohne Begründung und das Erlöskonto 4185
- Zahlungsart der Rechnung speichern: Die E-Rechnung sagt fest „58, SEPA-Überweisung“ und nur mit IBAN; die Felder dafür im Rechnungsformular entfielen mit 2.3.5, weil der Server sie nie las – braucht eine Spalte (Schemaänderung)
- Zurückholen einer versendeten Rechnung entfernt die Forderung auch nach einem DATEV-Export: Hinweis oder Sperre ab dem Export (braucht ein Merkmal „exportiert“ je Buchung; Schemaänderung)
- Löschen des Entwurfs einer Rechnung aus einem Angebot setzt das Angebot auf „angenommen“, nicht auf den Status davor: den früheren Status am Angebot merken (Schemaänderung)
- Datei zu einem bestehenden Beleg nachreichen (der Upload der Seite „Belege“ legt je Datei einen neuen Beleg an)
- Belegerkennung: Texterkennung (OCR) für gescannte PDFs und Fotos – bisher ist eine Datei ohne Textebene „nicht lesbar (kein Text)“
- Belegerkennung: ein lokales Sprachmodell als optionaler Nachleser für Belege, die die Textregeln nicht eindeutig lesen (standardmäßig aus; die Lesung bliebe ein Vorschlag mit Herkunft)
- Belegerkennung: den gelesenen Absender (Name, USt-IdNr.) einem Kontakt zuordnen – als Vorschlag, übernommen wird erst auf Klick
- Belegerkennung: aus einer Lesung einen Buchungssatz vorschlagen (Konto, Steuersatz, Betrag), gespeichert erst im Editor
- Belegerkennung: Vorschläge auch in der Gegenrichtung – an einer Buchung die passenden gelesenen Belege nennen (heute steht die Liste nur am Beleg)
- Belegerkennung, Vorschläge: die Abweichung zwischen Zahlbetrag und Brutto berücksichtigen (Skonto, Valuta) – heute zählt nur der gleiche Betrag auf den Cent
- Umsatzsteuer-Voranmeldung (ELSTER)
- Liquiditätsrechnung: einen unstimmigen Split neben einem Übertrag kenntlich machen – heute steht sein Rest nur nachrichtlich unter den Überträgen
- BWA: vom Nutzer pflegbare Gruppen (braucht Schemaänderung)
- Summen- und Saldenliste: Saldenvorträge bzw. Eröffnungsbilanz (heute beginnt ein Konto ohne Buchungen vor dem ersten Kontoauszug bei null; braucht Schemaänderung)
- EÜR: Zuordnung der Steuer- und Verbindlichkeitskonten überprüfen
- Soll-Versteuerung: Umsatzsteuer mit dem Leistungsdatum buchen und im DATEV-Stapel ausweisen (die Besteuerungsart ist seit 2.4.0 an der eigenen Firma gespeichert, wirkt aber noch nirgends)
- GoBD-Modus, je Datenverzeichnis einstellbar (Ersteinrichtung fragt: Steuerbüro oder selbst?); vor der Umsetzung ein Entwurf:
  - Festschreibung abgeschlossener Zeiträume, Korrektur nur per Storno/Gegenbuchung, kein Löschen
  - Änderungsprotokoll (wer/wann/alt/neu), manipulationssicher; SQL-Konsole nur lesend oder aus (seit 2.3.11 läuft sie ohne „Schreiben erlauben“ nur lesend – im GoBD-Modus fiele der Schreibmodus weg)
  - Gilt auch für Kasse und Fahrtenbuch; Abschalten nur protokolliert

## Kontenrahmen (SKR03/04/07 …)
Heute ist nur SKR04 ausgebaut, und auch dort nur eine Grundauswahl von Konten; die Auswahl
anderer Rahmen beim Anlegen eines Kontos ist bisher nur formal. Reihenfolge der Schritte:
1. **Funktionskonten statt fester Nummern** – Voraussetzung für alles Weitere; vor der Umsetzung ein Entwurf.
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
- DKB-CSV (`importers/dkb_csv.py`): nur an erfundenen Beispielen beider Formate geprüft, nicht an einer echten Datei – an echten Dateien (neues und altes Format) gegenprüfen: Spaltennamen, Kodierung, Beträge ohne Nachkommastellen, vorgemerkte Umsätze, Vorspann
- DATEV-Export vor der ersten Weitergabe ans Steuerbüro gegen einen echten Buchungsstapel prüfen: Seit 2.3.0 bildet er den Stapel von WISO nach (dreistellige BU-Schlüssel, Rechnungskette ohne Schlüssel mit dem Satz nur in Spalte 40, Belegfeld 1 bis 36 Zeichen, Version 700), ob die Kanzlei ihn so annimmt, ist offen. Nicht nachgebildet: Neuableitung alter Jahre aus der heutigen Bedeutung der Schlüssel, Schlüssel für innergemeinschaftlichen Erwerb, § 13b und steuerfreie Umsätze in der Ableitung (Importe bringen sie mit), Steuerzeilen, Generalumkehr; die Buchungsmaske zeigt und ändert den Schlüssel nicht
- Split-Summe (`is_bank_effective`, `find_unbalanced_splits`) kennt 1460 nur im Spiegel mit bekannter Lesart; bei einer Zahlung über 1460 und bei Spiegeln des Bestands (`mirror_legacy`) zählt sie nur die eigenen Bankkonten: „X an 1460“ als einziges Kind gilt bei Kasse ≠ 1460 als unausgeglichen (DATEV-Export gesperrt), bei Kasse = 1460 gilt „X an 1460“ plus „1460 an Bank“ als unausgeglichen – Regel je Zahlung neu fassen
- Dashboard zählt eine Umbuchung Anlage ↔ Erfolgskonto, der die Maske eine Kasse als Konto eingetragen hat, als Kassenbewegung (EÜR stimmt)
- Eigenständige WISO-Kassensätze auf 1600 fehlen in EÜR/Dashboard, solange die Kasse der Installation nicht 1600 trägt (ältere Installationen: Kasse ohne SKR oder 1460); die Kasse lässt sich nur vor der ersten Buchung auf 1600 stellen, danach bräuchte es eine Umstellung von Form und Vorzeichen mit Probelauf und Sicherung – nie als stille Migration
- Prüfliste für Spiegel und Umbuchungen des Bestands (Seite *Sonstiges*, nur lesend, mit Probelauf): Spiegel mit unbekannter Lesart (`mirror_legacy`; die Importe zählen sie als `legacy_mirrors`, ändern sie aber nicht) und Sätze ohne Geldseite, deren Konto inzwischen ein Geldkonto ist, mit Vorschlag und Wirkung (Soll/Haben im Export, Dashboard, Split-Summe) – sicher / wahrscheinlich / unklar; Übernehmen nur nach Bestätigung, vorher Sicherung, jedes UPDATE im Replay-Log, „unklar“ wird nie geändert
- Ein Importsatz ohne liquide Seite und mit leerem oder unbekanntem Gegenkonto (Form `transfer`, z. B. „6815 an –“) steht unter einer Bankbewegung in der EÜR ohne Vorsteuer: Die EÜR liest ihn als Umbuchung (Konto −netto), ein Zweckkonto für die Vorsteuer 1406 gibt es nicht
- Die Übernahme CSV → Datenbank erkennt eine Zeile auf Konten 1000–1099 aus 2.0.x (mit dem Betrag der alten Regel gespeichert) nur, wenn ihr Konto im Kontenrahmen stand (`COA_ID` gesetzt); fehlte es, entsteht eine zweite Buchung. Der Re-Import des Textexports erkennt auch diese Zeilen
- Umbuchungen Privat ↔ Erfolgskonto ohne Bankbezug (z. B. Kfz-Privatnutzung „2100 an 4645“, privat bezahlte Betriebsausgabe „6815 an 2180“, Entnahmewert eines Anlageguts) fehlen in der EÜR – fachlich mit dem Steuerbüro klären
- Alte AfA-Sätze (vor 2.0.5: Aufwandskonto auf einem Bank-/Kassenkonto, kein Gegenkonto) stehen im DATEV-Export ohne Gegenkonto; keine automatische Reparatur – Buchung löschen (setzt das Jahr zurück) und neu buchen
- Anlage wieder auf „aktiv“ setzen leert das Abgangsdatum nicht (AfA der Folgejahre bleibt gesperrt); Status „verschrottet“ ohne Datum sperrt nicht
- WISO-Textimport (CSV) nach einer in der App gebuchten AfA desselben Jahres zählt das Jahr doppelt (nur bei paralleler Nutzung beider Programme); der Datenbank-Import lässt den AfA-Satz eines in der App gebuchten Jahres aus (`depreciation_conflicts`)
- WISO-Datenbank-Import: Ein Satz ohne `ACCOUNTINGID` auf einem Unterkonto wird beim Re-Import über einen Import bis 2.2.4 ein zweites Mal angelegt (ohne Quellgruppe sucht die Übernahme am Konto: 6530 alt, 6532 neu); ob es solche Sätze gibt, ist an echten Daten zu prüfen
- AfA-Buchung nimmt als Aufwandskonto jedes Erfolgskonto 4000–7999 an, also auch SKR04-Erlöskonten (bewusst weit, weil selbst angelegte SKR03-Konten wie 4830 dort liegen) – mit den Funktionskonten enger fassen
- AfA im Abgangsjahr wird voll statt zeitanteilig geplant (der Plan endet mit dem Abgangsjahr, `calculate_depreciation_plan` kennt das Abgangsdatum aber nicht)
- Dreht der Nutzer eine AfA-Buchung der App im Editor zur Rückbuchung um, zählt das Jahr im Plan mit 0 als gebucht, in den Büchern aber negativ – die Summe der AfA liegt dann unter dem Anschaffungswert
- Die Vorschau im Anlagenformular rechnet nur die Formel und weicht bei fortgeschriebenen Plänen von der Plantabelle ab
- Die Fortschreibung des AfA-Plans (`depreciation_plan`) rechnet nach abweichender Geschichte (und für Erweiterungen) immer linear über die Restnutzungsdauer – keine degressive Fortschreibung, keine GWG-Regel; Lücken zwischen gebuchten Jahren bekommen keine eigene Zeile
- Ändert der Nutzer eine **importierte** AfA-Buchung nachträglich (Betrag im Editor), folgt der AfA-Plan nicht – zwischen Planjahr und importierter Buchung gibt es keine Verknüpfung (`Booking_ID` schreibt nur `book_depreciation`)
- Auto-Abgleich: Sätze, deren Richtung sich nur vermuten ließe (Umbuchung mit leerem oder unbekanntem Gegenkonto aus dem WISO-Textimport; Satz auf ein Konto, das erst später Bankkonto wurde), werden nicht mehr automatisch verknüpft und fehlen in der EÜR, bis sie im Editor zugeordnet sind – dafür fehlt eine Prüfliste mit „Übernehmen“; die Quellklammer-Stufen (0, 6b) rechnen solche Sätze weiter mit dem gespeicherten Betrag
- Auto-Abgleich: Eine Umbuchung ohne Geldseite (z. B. AfA am 31.12.) mit demselben Betrag am selben Tag zählt bei der Eindeutigkeit mit, auch wenn sie zu einem anderen Beleg gehört – die Zahlung bleibt dann unverknüpft, bis eine spätere Stufe sie findet; Bankbewegungen, deren Text vor 2.3.14 durch eine Kontonummer ersetzt wurde (Stufe 6), werden nicht gefunden und nicht repariert
- Dashboard: Die Kassenseite eines Übertrags, die nur als Kind unter der Bankbewegung steht (Abhebung über die Maske: „1600 an 1800“ unter der Giro-Bewegung), zählt nicht – mit dem Filter „nur Kasse“ fehlt die Bewegung, und bei einer Kasse auf 1460 (WISO-Paar „X an 1460“ plus „1460 an Bank“) bleibt „Überträge“ netto ≠ 0 und der Saldo doppelt; sie mitzuzählen stünde doppelt da, sobald die Kasse die Bewegung selbst führt – das ließe sich nur über die Paarungslogik des DATEV-Exports lösen
- Dashboard: Der unkontierte Rest einer Bankbewegung, die ein Spiegel-Kind zwischen eigenen Konten trägt, zählt als Übertrag (−150 mit Spiegel −100 und „6815 an Bank“ −30: Ausgabe −30, Übertrag −120) – ebenso ein Satz über das Verrechnungskonto 1460, der auf das Konto der Bewegung nicht wirkt; die Prüfung der Split-Summe meldet beides ohnehin
- Dashboard: Ein eigenständiger Spiegel des Altbestands (`mirror_legacy`) auf der Kasse deckt die Bankbewegung ohne Satz nicht (er wird nie gedeutet) – die Bankseite zählt bis zum Kontieren als Einnahme bzw. Ausgabe, die Kassenseite als Übertrag
- Dashboard: Die Erkennung der Gegenseite eines Übertrags ohne Satz setzt wie beim DATEV-Export vollständige Auszüge voraus – fehlt die echte Gegenseite eines kontierten Übertrags im Bestand (oder ist der Ausgang auf das falsche eigene Konto kontiert), kann eine gleich hohe Bewegung ohne Satz auf dem Zielkonto binnen 14 Tagen als Übertrag zählen, bis sie kontiert ist (sie steht unter „offen“)
- Dashboard: Eine Bankbewegung ohne Satz auf einem Konto ohne SKR-Nummer oder mit einer Nummer, die ein anderes eigenes Konto auch trägt, deckt die Übertragsrechnung nie (`own_transfer_accounts`) – ein Übertrag von oder zu so einem Konto zählt dort als Einnahme bzw. Ausgabe
- Bankbewegung ohne Editor speichern: Spiegel und automatisches Kind werden nach der Bankzeile in eigenen Schritten geschrieben – ein Fehler dort lässt die Bankzeile geändert zurück (das Speichern mit Editor läuft seit 2.3.15 in einer Transaktion)
- Löschungen (Buchung, Buchungssatz, Zuordnung) stehen nicht im SQL-Protokoll
- Bank-Editor: eine von Hand angelegte, später zugeordnete Buchung wird beim Entfernen der Zeile gelöscht (nicht von im Editor angelegten Zeilen unterscheidbar). Dasselbe gilt für Sätze aus dem WISO-Textexport (keine Quellklammer)
- Bank-Editor: Steuer einer zugeordneten Buchung lässt sich erst beim zweiten Speichern leeren
- Bank-Editor: Zuordnen eines Satzes mit leerem Konto vorne und Zweckkonto hinten ersetzt das Zweckkonto durch das Konto der Bank – stattdessen die leere Seite füllen oder den Satz unverändert lassen
- Bank-Editor: Das Gegenkonto eines zugeordneten Satzes mit Zweckkonto vorne lässt sich in der Zeile nicht ändern (nur über die Buchungsmaske)
- Bankmaske übernimmt den Kontakt des ersten Satzes auf eine Bankbewegung ohne Kontakt
- Ein eigenständiger Buchungssatz bekommt beim Speichern in der Maske das vorgewählte Bankkonto und gilt danach im Kontoauszug-Import als vorhandene Bankbewegung
- Kontoauszug-Import erkennt Duplikate nur über Konto + Datum + Betrag (je Datei gezählt, über Dateien das Maximum): Bankreferenz (AcctSvcrRef/EndToEndId) speichern, gebündelt mit den CAMT-Zusatzfeldern (Schemaänderung). Bis dahin: CAMT-Sammelbuchung gegen PDF-Summenzeile wird doppelt angelegt; zwei verschiedene Auszüge desselben Tags mit je einem echten, gleichen Umsatz verlieren einen
- WISO-Datenbank: Lesen der Satzversionen an einer großen echten Datei bestätigen (mehrere TIP-Seiten, Transaktionsnummern über 2³²) – geprüft ist es an zwei kleinen echten Firebird-3.0-Dateien
- Saldoprüfung der PDF-Auszüge: an echten Volksbank-Auszügen eines Kontos bestätigt; noch an keinem echten Auszug gesehen sind ein Soll-Saldo, ein einseitiger Auszug und ein Januar-Auszug mit Dezember-Buchungen des Vorjahres (Jahreswechsel-Regel)
- CAMT-Sammelbuchungen, deren Einzelbeträge nur unter `AmtDtls/TxAmt` stehen, bleiben ein Umsatz mit dem Gesamtbetrag: aufteilen erst zusammen mit einer Duplikaterkennung Summe ↔ Einzelbeträge (sonst legt der erneute Import eines alten Auszugs die Einzelbeträge zusätzlich an)
- DKB-PDF auf den Stand des Volksbank-Parsers bringen: ungültiges Datum wird dort noch zu „heute“, kein `unparsed`/`no_year`
- Vorschau des Kontoauszug-Imports nach einem Einzelimport neu berechnen – die übrigen Karten zeigen sonst veraltete „neu“-Zahlen (das Ergebnis des Imports stimmt)
- Kontoauszug-Import: Umsätze, die „Alle importieren“ bzw. der Einzelimport als Duplikat aus einer anderen Datei verwirft, lassen sich nicht gezielt doch importieren (z. B. zwei echte gleiche Beträge am selben Tag in zwei mitten am Tag geteilten Auszügen) – es fehlt eine Option „trotzdem importieren“; `skipped_upload` ist beim Einzelimport immer 0, die Meldung nennt dort nie „aus anderer Datei dieses Uploads“
- WISO-Datenbank-Import übernimmt CSV-Buchungen nur bei gleichem Beleg-Nr./Datum/Konto/Betrag **und Text** (das Gegenkonto darf nicht widersprechen); eine CSV-Zeile mit nachträglich geändertem Text, Gegenkonto, Betrag oder nachträglich gesetzter Beleg-Nr. (Verknüpfen eines Belegs, seit 2.5.12) wird nicht wiedererkannt (ein erneuter Import derselben Textdatei legt die Buchung ohne Meldung ein zweites Mal an, die Datenbank-Übernahme legt neu an und meldet ein mögliches Duplikat) – die WISO-Satz-Id als eigene Spalte würde das exakt machen (Schemaänderung, mit der Bankreferenz bündeln)
- Nicht wiedererkannte CSV-Zeilen bleiben als zweite Buchung stehen; auf Kasse/1460 und bei Umbuchungen Anlage ↔ Erfolgskonto zählen sie doppelt in der EÜR, bis eine gelöscht ist (Liste „möglicherweise doppelt“ auf der Seite Sonstiges)
- Der Weg „erst CSV, dann Datenbank“ nach der Änderung der Unterkonten (2.2.5) an einem echten CSV-/Datenbank-Paar erneut gegenprüfen (Texte, Umschlüsselung von Unterkonten)
- Der Datenbankweg liest Texte als latin-1 statt cp1252 (Anzeige von „€“ u. ä.; für den Vergleich mit der CSV umgesetzt, gespeichert wird der gelesene Text); „…“ (0x85) geht schon im Leser verloren (`_clean` fasst es als Leerraum zusammen) – solche Texte werden nicht wiedererkannt
- Neu angelegte Buchungen der WISO-Importe stehen nicht im SQL-Protokoll (nur die Übernahmen per UPDATE)
- Arbeitszeit: Ein Pausentext über Mitternacht („23:50-00:10“) rechnet 0 Minuten; die Überlappungsprüfung sieht Einträge der Nachbartage nicht (ein Eintrag bis 02:00 des Folgetags gegen einen Eintrag ab 01:00 dort)
- Fahrtenbuch: Fahrzeuge sind Freitext an der Fahrt – eine Fahrzeug-Stammliste (mit gespeicherter Einstellung, ob für das Fahrzeug die Lückenprüfung gelten soll) wäre eine Schemaänderung
- Beträge: Anlagenformular (`purchase_price`/`sale_price`), die übrigen Betragsfelder der Formulare (`_finite_number`: als `float()` nimmt es auch Ziffern anderer Schriften und `1_000`) und die Saldosuche der PDF-Auszüge (`importers/pdftext.py`) lesen noch nicht über `money.parse_amount`
- Kontaktformular: Bei einer abgelehnten Eingabe (doppelte Kunden-/Kontaktnummer, doppeltes Kürzel, ungültige Angabe) gehen die Eingaben verloren – die Weiterleitung mit Meldung öffnet das Formular leer bzw. neu geladen
- SQL-Protokoll: `log_sql` schreibt jeden Befehl einzeilig und fasst dabei Leerraum auch innerhalb von Zeichenketten zusammen – ein Text mit Zeilenumbruch oder doppelten Leerzeichen steht im Protokoll anders als in der Datenbank (betrifft auch die Befehle der SQL-Konsole)
- WISO-Datenbank-Import: Eine Änderung innerhalb einer weiter bestehenden Buchungsgruppe (anderer Betrag, entfallene Teilbuchung) bemerkt der Re-Import nicht – die vorhandene Zeile bleibt, wie sie ist
- WISO-Datenbank-Import: Nicht mehr vorhandene und doppelt angelegte Buchungen (in WISO gelöscht oder neu angelegt, Altbestand bis 2.2.4 auf dem Basiskonto eines Unterkontos, Zeilen aus 2.0.x auf 1000–1099, neu gerechneter AfA-Plan unter neuer Gruppe) lassen sich nur von Hand bereinigen – ein Werkzeug mit Probelauf und Sicherung fehlt
- Index auf `Bookings.SourceGroup` anlegen (Schemaänderung): Der Re-Import des WISO-Datenbankimports sucht darüber
- Eigene Firma als Person hat keine Felder für DATEV-Berater- und Mandantennummer (nur das Firmenformular führt sie); der WISO-Import übernimmt sie dann nicht (nur ein Hinweis)
- Anlagenformular einer Erweiterung verlangt weiter Nutzungsdauer und Methode, obwohl die der Hauptanlage gelten
- Rechnungsformular (`/invoice?id=…`) läuft bei 1440 px Breite rechts aus dem Viewport
- Invoices-Zeilen werden per Spaltenindex gelesen (noch rund 50 Stellen in `server/pages_invoice.py`; `InvoiceCol` gibt es, genutzt in `server/pages.py`) – bricht bei Schemaänderungen, besser Spaltennamen
- Hinweis auf § 19 UStG: aus WISO importierte Kleinunternehmer-Rechnungen tragen 0 % statt des Platzhalters „ohne Steuerausweis“ – ein für sie nachträglich erzeugtes Dokument bekommt den Hinweis nicht; eine Einstellung „Kleinunternehmer“ an der eigenen Firma wäre besser als die Vorbelegung nach der letzten Rechnung
- Logo: Die Dateiauswahl im Kontaktformular lässt auch SVG/BMP wählen, der Server lehnt sie ab → Auswahl auf die erlaubten Formate begrenzen; WEBP braucht eine Pillow-Version mit WEBP-Unterstützung
- DATEV-Export, Überträge zwischen eigenen Konten: Die Erkennung beim Export zählt und rät im Zweifel nicht (Grenzen in `DB_MODEL.md`: Laufzeit über 14 Tage, Serien mit einer Lücke, Stückelung, Konten mit gleicher SKR-Nummer) – sauberer wäre, die beiden Bankbewegungen eines Übertrags ausdrücklich zu verknüpfen
- DATEV-Export: Ein Betrag, der in der Datenbank als Text steht (nur über die SQL-Konsole möglich), lässt die Split-Prüfung mit einer technischen Meldung scheitern
- `pdfplumber` ist derzeit Pflicht: Die PDF-Parser (seit 2.6.0 auch `receipt_reading/pdf_text.py`) importieren es beim Start, ohne das Paket startet die App nicht – Import verzögern, damit CAMT-Import und alles andere ohne es laufen
- Hinweisdateien für Entwicklungswerkzeuge (`CLAUDE.md`, `AGENTS.md`) auf Doppelungen prüfen und kürzen
- Einheiten auf Artikel-Listen/-Stammdaten prüfen (lesbar statt UN/ECE-Code)
- Rechnung speichern scheiterte einmal im Docker-Mehrbenutzerbetrieb mit „FOREIGN KEY constraint failed“ (DB danach sauber, gleicher Request lokal ok, nicht mehr reproduzierbar) – ab 2.0.1 nennt die Meldung den Verweis; bei Wiederauftreten Meldung und `docker logs` auswerten
- PDF-Kopf ans Formular angleichen: „Rechnungsdatum“ (heute „Datum:“), Leistungsdatum direkt darunter – Wertspalte für das längere Label verschieben, Golden-Dateien neu erzeugen
