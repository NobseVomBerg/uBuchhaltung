# Offene Aufgaben

Kurzliste, öffentlich. Erledigtes wird gelöscht, nicht abgehakt – die Historie
steht in `git log`. Eine Zeile je Punkt; Details gehören in Commit oder Issue.

## Repo & Veröffentlichung
- awesome-selfhosted: frühestens ab 4.3.2027 (erster Release `v1.6.16` am 3.11.2026); Eintrag schreibt der Owner selbst (Listenregel: keine KI-generierten Beiträge)
- Docker-Demo (`-e UBUCHHALTUNG_DEMO=1`) einmal im echten Container prüfen und den Umzug einer 1.x-Installation auf 2.0.0

## Funktionen
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
- Rechnungsformular (`/invoice?id=…`) läuft bei 1440 px Breite rechts aus dem Viewport
- Invoices-Zeilen werden per Spaltenindex gelesen (u. a. `server/pages_invoice.py`) – bricht bei Schemaänderungen, besser Spaltennamen
- CI testet nur Python 3.13, README nennt 3.10+ → Matrix ergänzen oder Angabe anpassen
- KI-Doku entschlacken: `CLAUDE.md` und `AGENTS.md` auf Doppelungen prüfen
- Einheiten auf Artikel-Listen/-Stammdaten prüfen (lesbar statt UN/ECE-Code)
- Pfadprüfung beim Ausliefern gespeicherter Rechnungen (`stored_invoice_document`) – Security-Review, niedrige Priorität
- Rechnung speichern scheiterte einmal im Docker-Mehrbenutzerbetrieb mit „FOREIGN KEY constraint failed“ (DB danach sauber, gleicher Request lokal ok, nicht mehr reproduzierbar) – ab 2.0.1 nennt die Meldung den Verweis; bei Wiederauftreten Meldung und `docker logs` auswerten
- Angebot speichern wie Rechnungen in einer Transaktion (Kopf + Positionen) – heute einzeln committet, ein Fehler hinterlässt halbe Belege
- PDF-Kopf ans Formular angleichen: „Rechnungsdatum“, Leistungsdatum direkt darunter – Wertspalte für das längere Label verschieben, Golden-Dateien neu erzeugen
- Seit 1.6.20 scheitern lokal unter Windows 20 Tests (`test_http_guard`, `test_sql_console`, `test_test_isolation`, `test_cii_invoice`, `test_demo`, `test_db`, `test_invoice_booking_link`: 500 statt 303 bzw. „no such table“) – auch ohne lokale Änderungen; CI (Linux) prüfen
