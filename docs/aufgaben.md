# Offene Aufgaben

Kurzliste, öffentlich. Erledigtes wird gelöscht, nicht abgehakt – die Historie
steht in `git log`. Eine Zeile je Punkt; Details gehören in Commit, Issue oder `docs/regeln/`.

## Repo & Veröffentlichung
- Docker-Demo (`-e UBUCHHALTUNG_DEMO=1`) einmal im echten Container prüfen, ebenso den Umzug einer 1.x-Installation

## Funktionen
- Sammelimport: Exportdateien und Kontoauszüge als Sammlung hineingeben, ohne auf Importreihenfolgen zu achten – Spalten frei gewählter Exporte erkennen und in einer Vorschau zuordnen, ein gemeinsamer Abgleich mit sichtbaren Zweifelsfällen; braucht Schemaänderung und vorher einen Entwurf
- Kontoauszüge im MT940-Format (heute gelesen: CAMT.053, PDF von DKB und Volksbank Rottweil, CSV der DKB)
- Kontoauszug als CSV anderer Banken (heute liest `importers/dkb_csv.py` nur die Umsatzliste der DKB)
- Rechnungen mit mehreren Steuersätzen oder mit einem Erlöskonto aus den Stammdaten „Steuersätze“: Zahlungsumzug auf eine Ersatzrechnung und „Zuordnung neu aufbauen“ (Gründe `mixed`, `tax_entry`) lassen sie aus – je Satz bzw. mit dem Konto des Eintrags neu buchen, mit denselben Bedingungen wie bei einem Satz
- Steuersätze aus den Stammdaten, offen: Wartekonto für weitere Sätze (feste Kontonummer je Satz); BU-Schlüssel für weitere Sätze und steuerfreie Umsätze im DATEV-Export; Befreiungscode der E-Rechnung (BT-121); eigene Steuerkonten für 5 % und 16 % (zweites Halbjahr 2020)
- WISO-Datenbank-Import: Rechnungen mit mehreren Steuersätzen je Position übernehmen (heute ein Satz im Kopf, die Prüfliste nennt sie `unflagged_rate_mix`)
- Leistungszeitraum im Angebot (heute nur in der Rechnung)
- Lastschrift (59) in der E-Rechnung: Mandatsreferenz, Gläubiger-ID und IBAN des Kunden (BR-DE-25, -30, -31) – heute gibt es keine Zahlungsart 59
- Karte in der XRechnung: Ziffern der Kartennummer (BT-87, BR-DE-24) – heute lässt die Karte das UBL aus, die CII im PDF entsteht
- Skonto bei der Zahlungszuordnung, offen: Rechnungen mit Steuersatz je Position oder Satz aus den Stammdaten, andere Kopfsätze als 19 %, 7 % und 0 %; Kleindifferenzen und Nachlässe ohne Skonto-Bedingung; Darstellung im DATEV-Stapel (zweite Bankzeile) vor der ersten Weitergabe mit dem Steuerbüro klären
- Kleinunternehmer-Erlöse (4184 ab 2026), offen: ausgewiesene 0 % ohne Eintrag der Stammdaten buchen weiter auf 4185; ältere Zahlungen ab 2026 auf 4185 bleiben dort (Umstellung mit Probelauf fehlt)
- Zahlungsvorschläge, offen: Teilbeträge, Wahl des Erlöskontos im Kasten, von der Bank mitten in den Ziffern umbrochene Rechnungsnummern, andere Währungen als Euro
- Zuordnung neu aufbauen, Schritt 2: markierte Zahlungssätze ohne Zuordnung (wiederherstellen oder entfernen, Wahl im Probelauf) und Sätze „Bank an Debitoren“ ohne Forderung
- Steuerfälle bei Rechnungen und Angeboten: Reverse Charge, innergemeinschaftliche Lieferung und Ausfuhr (eigene E-Rechnungs-Kategorien, USt-IdNr. des Käufers als Pflicht, eigene Erlöskonten); 0 % ohne Eintrag der Stammdaten und ohne § 19 ergibt weiter Kategorie Z ohne Begründung und Konto 4185
- Merkmal „exportiert“ (`BookingExports`): weitere Wirkungen (Storno von Hand, Zahlung entfernen, Bank-Editor, Festschreiben) und Ablösung des Stempels `DateTax` beim DATEV-Export; bis dahin nennt der Umzug der Zahlungen für exportierte Zahlungssätze den Grund „von Hand geändert oder ergänzt“ statt „anderes Steuerdatum (z. B. nach einem DATEV-Export)“
- Datei zu einem bestehenden Beleg nachreichen (der Upload der Seite „Belege“ legt je Datei einen neuen Beleg an)
- Belegerkennung: Texterkennung (OCR) für gescannte PDFs und Fotos – eine Datei ohne Textebene ist „nicht lesbar (kein Text)“
- Belegerkennung: ein lokales Sprachmodell als optionaler Nachleser für nicht eindeutig gelesene Belege (standardmäßig aus; die Lesung bliebe ein Vorschlag mit Herkunft)
- Belegerkennung: den gelesenen Absender (Name, USt-IdNr.) einem Kontakt zuordnen – als Vorschlag, übernommen erst auf Klick
- Belegerkennung: aus einer Lesung einen Buchungssatz vorschlagen (Konto, Steuersatz, Betrag), gespeichert erst im Editor
- Belegerkennung: Vorschläge auch in der Gegenrichtung – an einer Buchung die passenden gelesenen Belege nennen
- Belegerkennung, Vorschläge: die Abweichung zwischen Zahlbetrag und Brutto berücksichtigen (Skonto, Valuta) – heute zählt nur der gleiche Betrag auf den Cent
- Umsatzsteuer-Voranmeldung (ELSTER)
- Liquiditätsrechnung: einen unstimmigen Split neben einem Übertrag kenntlich machen – sein Rest steht nur nachrichtlich unter den Überträgen
- BWA: vom Nutzer pflegbare Gruppen (braucht Schemaänderung)
- Summen- und Saldenliste: Saldenvorträge bzw. Eröffnungsbilanz (ein Konto ohne Buchungen vor dem ersten Kontoauszug beginnt bei null; braucht Schemaänderung)
- EÜR: Zuordnung der Steuer- und Verbindlichkeitskonten überprüfen
- Soll-Versteuerung: Umsatzsteuer mit dem Leistungsdatum buchen und im DATEV-Stapel ausweisen (die Besteuerungsart ist an der eigenen Firma gespeichert, wirkt aber noch nirgends)
- GoBD-Modus, je Datenverzeichnis einstellbar (Ersteinrichtung fragt: Steuerbüro oder selbst?); vor der Umsetzung ein Entwurf:
  - Festschreibung abgeschlossener Zeiträume, Korrektur nur per Storno/Gegenbuchung, kein Löschen
  - Änderungsprotokoll (wer/wann/alt/neu), manipulationssicher; SQL-Konsole nur lesend oder aus
  - Gilt auch für Kasse und Fahrtenbuch; Abschalten nur protokolliert

## Kontenrahmen (SKR03/04/07 …)
Heute ist nur SKR04 ausgebaut, und auch dort nur eine Grundauswahl; die Auswahl anderer Rahmen beim Anlegen eines Kontos ist bisher nur formal. Reihenfolge der Schritte:
1. **Funktionskonten statt fester Nummern** – Voraussetzung für alles Weitere; vor der Umsetzung ein Entwurf. Rund 70 fest verdrahtete SKR04-Nummern in 12 Dateien (z. B. 3806, 1401/1406, 4400, Wartekonten 4405/4345/4340, Debitoren 10000) werden Rollen wie „Umsatzsteuer 19 %“ oder „Wartekonto“ je Kontenrahmen
2. **Vollständige Kontenrahmen als Seed-Daten** (`seed_data/chart_of_accounts_<rahmen>.json`), zuerst SKR03; Quelle und Nutzungsrechte der Kontenlisten vorher klären
3. **Übersicht trotz vieler Konten:** Auswahllisten zeigen nur sichtbare Konten (`ShowInMenu` gibt es schon); Grundauswahl je Rahmen, Suche, Gruppierung nach Kontenklasse, zuletzt genutzte oben
4. **Wahl des Kontenrahmens bei der Ersteinrichtung**
5. **Umstellung bestehender Datenbanken** (z. B. SKR03 → SKR04): braucht ein vollständiges Mapping, das nicht überall 1:1 ist; Sicherung, Probelauf mit Bericht, nicht eindeutige Konten melden statt raten
- SKR07 (Österreich) erst, wenn die Steuerlogik dafür ansteht – bewusst später

## Fehler & Technik
- DKB-CSV (`importers/dkb_csv.py`): nur an erfundenen Beispielen geprüft – an echten Dateien (neues und altes Format) gegenprüfen: Spaltennamen, Kodierung, Beträge ohne Nachkommastellen, vorgemerkte Umsätze, Vorspann
- DATEV-Export vor der ersten Weitergabe gegen einen echten Buchungsstapel prüfen; ob die Kanzlei ihn annimmt, ist offen. Nicht nachgebildet: Neuableitung alter Jahre, Schlüssel für innergemeinschaftlichen Erwerb, § 13b und steuerfreie Umsätze in der Ableitung, Steuerzeilen, Generalumkehr; die Buchungsmaske zeigt und ändert den Schlüssel nicht
- Split-Summe (`is_bank_effective`, `find_unbalanced_splits`) kennt 1460 nur im Spiegel mit bekannter Lesart: „X an 1460“ als einziges Kind gilt bei Kasse ≠ 1460 als unausgeglichen (DATEV-Export gesperrt), bei Kasse = 1460 gilt „X an 1460“ plus „1460 an Bank“ als unausgeglichen – Regel je Zahlung neu fassen
- Dashboard zählt eine Umbuchung Anlage ↔ Erfolgskonto, der die Maske eine Kasse als Konto eingetragen hat, als Kassenbewegung (EÜR stimmt)
- Eigenständige WISO-Kassensätze auf 1600 fehlen in EÜR/Dashboard, solange die Kasse der Installation nicht 1600 trägt; Umstellung nur mit Probelauf und Sicherung, nie als stille Migration
- Prüfliste für Spiegel und Umbuchungen des Bestands (Seite *Daten*, nur lesend, mit Probelauf): Spiegel mit unbekannter Lesart (`mirror_legacy`) und Sätze ohne Geldseite auf einem inzwischen liquiden Konto, mit Vorschlag und Wirkung; Übernehmen nur nach Bestätigung und Sicherung, „unklar“ wird nie geändert
- Ein Importsatz ohne liquide Seite und mit leerem oder unbekanntem Gegenkonto (Form `transfer`, z. B. „6815 an –“) steht unter einer Bankbewegung in der EÜR ohne Vorsteuer: Die EÜR liest ihn als Umbuchung, ein Zweckkonto für die Vorsteuer 1406 gibt es nicht
- Die Übernahme CSV → Datenbank erkennt eine Zeile auf Konten 1000–1099 aus 2.0.x nur, wenn ihr Konto im Kontenrahmen stand (`COA_ID` gesetzt); sonst entsteht eine zweite Buchung. Der Re-Import des Textexports erkennt sie
- Umbuchungen Privat ↔ Erfolgskonto ohne Bankbezug (z. B. Kfz-Privatnutzung „2100 an 4645“, „6815 an 2180“) fehlen in der EÜR – fachlich mit dem Steuerbüro klären
- Alte AfA-Sätze (vor 2.0.5: Aufwandskonto auf einem Bank-/Kassenkonto, kein Gegenkonto) stehen im DATEV-Export ohne Gegenkonto; keine automatische Reparatur – Buchung löschen und neu buchen
- Anlage wieder auf „aktiv“ setzen leert das Abgangsdatum nicht (AfA der Folgejahre bleibt gesperrt); Status „verschrottet“ ohne Datum sperrt nicht
- WISO-Textimport nach einer in der App gebuchten AfA desselben Jahres zählt das Jahr doppelt (nur bei paralleler Nutzung beider Programme); der Datenbank-Import lässt den Satz aus (`depreciation_conflicts`)
- WISO-Datenbank-Import: Ein Satz ohne `ACCOUNTINGID` auf einem Unterkonto wird beim Re-Import über einen Import bis 2.2.4 ein zweites Mal angelegt; ob es solche Sätze gibt, ist an echten Daten zu prüfen
- AfA-Buchung nimmt als Aufwandskonto jedes Erfolgskonto 4000–7999 an, auch SKR04-Erlöskonten (bewusst weit, wegen selbst angelegter SKR03-Konten wie 4830) – mit den Funktionskonten enger fassen
- AfA im Abgangsjahr wird voll statt zeitanteilig geplant (`calculate_depreciation_plan` kennt das Abgangsdatum nicht)
- Dreht der Nutzer eine AfA-Buchung der App im Editor zur Rückbuchung um, zählt das Jahr im Plan mit 0 als gebucht, in den Büchern aber negativ – die AfA-Summe liegt dann unter dem Anschaffungswert
- Die Vorschau im Anlagenformular rechnet nur die Formel und weicht bei fortgeschriebenen Plänen von der Plantabelle ab
- Die Fortschreibung des AfA-Plans (`depreciation_plan`) rechnet nach abweichender Geschichte immer linear über die Restnutzungsdauer – keine degressive Fortschreibung, keine GWG-Regel; Lücken zwischen gebuchten Jahren bekommen keine eigene Zeile
- Ändert der Nutzer eine **importierte** AfA-Buchung nachträglich, folgt der AfA-Plan nicht – es gibt keine Verknüpfung zwischen Planjahr und importierter Buchung
- Auto-Abgleich: Sätze mit nur vermutbarer Richtung (Umbuchung mit leerem oder unbekanntem Gegenkonto aus dem WISO-Textimport; Konto, das erst später Bankkonto wurde) werden nicht automatisch verknüpft und fehlen in der EÜR, bis sie im Editor zugeordnet sind – es fehlt eine Prüfliste mit „Übernehmen“
- Auto-Abgleich: Eine Umbuchung ohne Geldseite (z. B. AfA am 31.12.) mit demselben Betrag am selben Tag zählt bei der Eindeutigkeit mit, auch wenn sie zu einem anderen Beleg gehört – die Zahlung bleibt dann unverknüpft, bis eine spätere Stufe sie findet; Bankbewegungen mit durch eine Kontonummer ersetztem Text (Stufe 6) werden nicht gefunden
- Dashboard: Die Kassenseite eines Übertrags, die nur als Kind unter der Bankbewegung steht („1600 an 1800“ unter der Giro-Bewegung), zählt nicht – mit dem Filter „nur Kasse“ fehlt die Bewegung, und bei Kasse auf 1460 bleiben „Überträge“ netto ≠ 0 und der Saldo doppelt; Lösung nur über die Paarungslogik des DATEV-Exports
- Dashboard: Der unkontierte Rest einer Bankbewegung, die ein Spiegel-Kind zwischen eigenen Konten trägt, zählt als Übertrag (−150 mit Spiegel −100 und „6815 an Bank“ −30: Ausgabe −30, Übertrag −120), ebenso ein Satz über 1460, der auf das Konto der Bewegung nicht wirkt; die Split-Prüfung meldet beides
- Dashboard: Ein eigenständiger Spiegel des Altbestands (`mirror_legacy`) auf der Kasse deckt die Bankbewegung ohne Satz nicht – die Bankseite zählt bis zum Kontieren als Einnahme bzw. Ausgabe, die Kassenseite als Übertrag
- Dashboard: Die Erkennung der Gegenseite eines Übertrags ohne Satz setzt wie beim DATEV-Export vollständige Auszüge voraus – fehlt die echte Gegenseite eines kontierten Übertrags, kann eine gleich hohe Bewegung auf dem Zielkonto binnen 14 Tagen als Übertrag zählen, bis sie kontiert ist
- Dashboard: Eine Bankbewegung ohne Satz auf einem Konto ohne SKR-Nummer oder mit einer Nummer, die ein anderes eigenes Konto auch trägt, deckt die Übertragsrechnung nie (`own_transfer_accounts`) – ein Übertrag von oder zu so einem Konto zählt als Einnahme bzw. Ausgabe
- Bankbewegung ohne Editor speichern: Spiegel und automatisches Kind werden nach der Bankzeile in eigenen Schritten geschrieben – ein Fehler dort lässt die Bankzeile geändert zurück (mit Editor läuft das Speichern in einer Transaktion)
- Löschungen (Buchung, Buchungssatz, Zuordnung) stehen nicht im SQL-Protokoll
- Bank-Editor: eine von Hand angelegte, später zugeordnete Buchung wird beim Entfernen der Zeile gelöscht (nicht von im Editor angelegten Zeilen unterscheidbar); dasselbe gilt für Sätze aus dem WISO-Textexport
- Bank-Editor: Steuer einer zugeordneten Buchung lässt sich erst beim zweiten Speichern leeren
- Bank-Editor: Zuordnen eines Satzes mit leerem Konto vorne und Zweckkonto hinten ersetzt das Zweckkonto durch das Konto der Bank – stattdessen die leere Seite füllen oder den Satz unverändert lassen
- Bank-Editor: Das Gegenkonto eines zugeordneten Satzes mit Zweckkonto vorne lässt sich in der Zeile nicht ändern (nur über die Buchungsmaske)
- Bankmaske übernimmt den Kontakt des ersten Satzes auf eine Bankbewegung ohne Kontakt
- Ein eigenständiger Buchungssatz bekommt beim Speichern in der Maske das vorgewählte Bankkonto und gilt danach im Kontoauszug-Import als vorhandene Bankbewegung
- Kontoauszug-Import: Eine CAMT-Sammelbuchung wird gegen eine PDF-Summenzeile doppelt angelegt (die PDF-Seite trägt keine Bankreferenz). Zwei Auszüge desselben Tags mit je einem echten, gleichen Umsatz verlieren einen: Tragen beide eine verschiedene Bankreferenz, markiert die Vorschau die Zeile und sie lässt sich erzwingen, sonst nicht
- WISO-Datenbank: Lesen der Satzversionen an einer großen echten Datei bestätigen (mehrere TIP-Seiten, Transaktionsnummern über 2³²) – geprüft an zwei kleinen echten Firebird-3.0-Dateien
- Saldoprüfung der PDF-Auszüge: noch an keinem echten Auszug gesehen sind ein Soll-Saldo, ein einseitiger Auszug und ein Januar-Auszug mit Dezember-Buchungen des Vorjahres
- CAMT-Sammelbuchungen, deren Einzelbeträge nur unter `AmtDtls/TxAmt` stehen, bleiben ein Umsatz mit dem Gesamtbetrag: aufteilen erst zusammen mit einer Duplikaterkennung Summe ↔ Einzelbeträge
- DKB-PDF auf den Stand des Volksbank-Parsers bringen: ungültiges Datum wird dort noch zu „heute“, kein `unparsed`/`no_year`
- Vorschau des Kontoauszug-Imports nach einem Einzelimport neu berechnen – die übrigen Karten zeigen sonst veraltete „neu“-Zahlen (das Ergebnis des Imports stimmt)
- Kontoauszug-Import: Ein als Duplikat verworfener Umsatz ohne Bankreferenz (PDF, CSV) lässt sich nicht gezielt doch importieren; `skipped_upload` ist beim Einzelimport immer 0
- WISO-Datenbank-Import übernimmt CSV-Buchungen nur bei gleichem Beleg-Nr./Datum/Konto/Betrag **und Text**; eine nachträglich geänderte Zeile wird nicht wiedererkannt (der Textexport legt sie ohne Meldung doppelt an, die Datenbank-Übernahme meldet ein mögliches Duplikat) – die WISO-Satz-Id als eigene Spalte würde das exakt machen (Schemaänderung)
- Nicht wiedererkannte CSV-Zeilen bleiben als zweite Buchung stehen; auf Kasse/1460 und bei Umbuchungen Anlage ↔ Erfolgskonto zählen sie doppelt in der EÜR, bis eine gelöscht ist (Liste „möglicherweise doppelt“ auf der Seite Daten)
- Der Weg „erst CSV, dann Datenbank“ nach der Änderung der Unterkonten an einem echten CSV-/Datenbank-Paar gegenprüfen (Texte, Umschlüsselung von Unterkonten)
- Der Datenbankweg liest Texte als latin-1 statt cp1252 (Anzeige von „€“ u. ä.); „…“ (0x85) geht schon im Leser verloren (`_clean`) – solche Texte werden nicht wiedererkannt
- Neu angelegte Buchungen der WISO-Importe stehen nicht im SQL-Protokoll (nur die Übernahmen per UPDATE)
- Arbeitszeit: Ein Pausentext über Mitternacht („23:50-00:10“) rechnet 0 Minuten; die Überlappungsprüfung sieht Einträge der Nachbartage nicht
- Fahrtenbuch: Fahrzeuge sind Freitext an der Fahrt – eine Fahrzeug-Stammliste (mit Einstellung für die Lückenprüfung) wäre eine Schemaänderung
- Beträge: Anlagenformular (`purchase_price`/`sale_price`), die übrigen Betragsfelder der Formulare (`_finite_number` nutzt `float()` und nimmt auch Ziffern anderer Schriften und `1_000`) und die Saldosuche der PDF-Auszüge (`importers/pdftext.py`) lesen noch nicht über `money.parse_amount`
- Kontaktformular: Bei einer abgelehnten Eingabe (doppelte Nummer oder Kürzel, ungültige Angabe) gehen die Eingaben verloren – das Formular öffnet leer bzw. neu geladen
- SQL-Protokoll: `log_sql` fasst Leerraum auch innerhalb von Zeichenketten zusammen – ein Text mit Zeilenumbruch oder doppelten Leerzeichen steht im Protokoll anders als in der Datenbank (auch bei der SQL-Konsole)
- WISO-Datenbank-Import: Eine Änderung innerhalb einer weiter bestehenden Buchungsgruppe (anderer Betrag, entfallene Teilbuchung) bemerkt der Re-Import nicht
- WISO-Datenbank-Import: In WISO gelöschte oder neu angelegte Buchungen, Altbestand auf dem Basiskonto eines Unterkontos, Zeilen aus 2.0.x auf 1000–1099 und ein neu gerechneter AfA-Plan unter neuer Gruppe lassen sich nur von Hand bereinigen – ein Werkzeug mit Probelauf und Sicherung fehlt
- Eigene Firma als Person hat keine Felder für DATEV-Berater- und Mandantennummer (nur das Firmenformular führt sie); der WISO-Import übernimmt sie dann nicht (nur ein Hinweis)
- Anlagenformular einer Erweiterung verlangt weiter Nutzungsdauer und Methode, obwohl die der Hauptanlage gelten
- Invoices-Zeilen werden per Spaltenindex gelesen (viele Stellen in `server/pages_invoice.py`; `InvoiceCol` gibt es, genutzt in `server/pages.py`) – bricht bei Schemaänderungen, besser Spaltennamen
- Hinweis auf § 19 UStG: aus WISO importierte Kleinunternehmer-Rechnungen tragen 0 % statt des Platzhalters „ohne Steuerausweis“ – ein nachträglich erzeugtes Dokument bekommt den Hinweis nicht; besser wäre eine Einstellung „Kleinunternehmer“ an der eigenen Firma
- Logo: Die Dateiauswahl im Kontaktformular (`accept="image/*"`) lässt auch SVG/BMP wählen, der Server lehnt sie ab → Auswahl auf die erlaubten Formate begrenzen; WEBP braucht eine Pillow-Version mit WEBP-Unterstützung
- DATEV-Export, Überträge zwischen eigenen Konten: Die Erkennung zählt und rät im Zweifel nicht (Grenzen in `DB_MODEL.md`) – sauberer wäre, die beiden Bankbewegungen eines Übertrags ausdrücklich zu verknüpfen
- DATEV-Export: Ein Betrag, der in der Datenbank als Text steht (nur über die SQL-Konsole möglich), lässt die Split-Prüfung mit einer technischen Meldung scheitern
- `pdfplumber` ist Pflicht: Die PDF-Parser (`importers/vbr.py`, `receipt_reading/pdf_text.py`) importieren es beim Start – Import verzögern, damit CAMT-Import und alles andere ohne es laufen
- Rechnung speichern scheiterte einmal im Docker-Mehrbenutzerbetrieb mit „FOREIGN KEY constraint failed“ (nicht reproduzierbar); bei Wiederauftreten Meldung und `docker logs` auswerten
