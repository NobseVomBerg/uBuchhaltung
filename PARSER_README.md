# Kontoauszug-Import & WISO-CSV-Import

## 1. Kontoauszüge (PDF und CAMT.053)

### Installation

```bash
pip install pdfplumber
```

Optional für OCR (gescannte PDFs):
```bash
pip install pytesseract pdf2image Pillow
```

Optional für PDF-Rechnungen mit Logo:
```bash
pip install Pillow
```

### Aufbau: Abstraktionsschicht + Bankmodule

Der Import ist zweigeteilt. Die Anwendung kennt nur die Abstraktionsschicht,
die Bankeigenheiten stecken je in einem eigenen Modul unter `importers/`:

| Datei | Aufgabe |
|---|---|
| `importers/base.py` | Gemeinsames Datenmodell: `BankStatement` mit `StatementTransaction`-Zeilen |
| `importers/pdftext.py` | Extraktions-Helfer für alle Module: Text, IBAN, Belegdatum |
| `importers/plausibility.py` | Zentrale Inhaltsprüfung (siehe unten) |
| `importers/vbr.py` | Volksbank Rottweil (PDF) |
| `importers/dkb.py` | DKB (PDF) |
| `importers/__init__.py` | Registry `IMPORTERS`, `find_importer()`, `parse_statement()` |

Die Spalten einer importierten Bewegung (`CANONICAL_FIELDS`) sind bewusst
knapp: `date`, `amount`, `recipient`, `reference`, `foreign_iban`. Mehr braucht
eine Bankbewegung nicht, und mehr muss ein Bankmodul nicht liefern.

**Neue Bank anbinden:** ein Modul mit einer `StatementImporter`-Klasse
schreiben (`detect()` erkennt den Beleg, `parse()` liefert den
`BankStatement`) und die Klasse in `IMPORTERS` eintragen. Am übrigen Code
ändert sich nichts.

### Plausibilitätsprüfung

PDF-Textextraktion ist ungenau: Kopf- und Fußzeilen rutschen in Empfänger oder
Verwendungszweck, Beträge verschwinden, Jahreszahlen verrutschen. Nach dem
Parsen läuft deshalb `plausibility.check_statement()` über jeden Beleg und
vermerkt Befunde an den Bewegungen – ohne den Import zu blockieren; die
Vorschau zeigt sie, entschieden wird vom Nutzer.

| Befund | Bedeutung |
|---|---|
| `amount` | Betrag fehlt oder ist 0 |
| `date` | Datum fehlt oder ist unlesbar |
| `daterange` | Datum liegt weit neben dem Belegdatum (Jahresdreher) |
| `empty` | weder Empfänger noch Verwendungszweck |
| `boiler` | Inhalt ist vollständig Kopf-/Fußzeilentext ("Seite 2 von 5", "Übertrag auf Blatt 2", "Neuer Kontostand", Formularnummern) |
| `huge` | Betrag über 1.000.000 – fast immer ein verrutschtes Trennzeichen |

Am Beleg selbst: `no_transactions`, `no_iban`, `parse_error`.

Die `boiler`-Muster greifen nur auf das **ganze** Feld: "Kontoauszug-Gebühr
Januar" oder "Blattwerk Gartenbau" bleiben unauffällig, "Blatt 3" nicht. Und
eine Zeile mit brauchbarem Verwendungszweck wird nicht wegen eines kruden
Empfängers markiert.

### Funktionsweise

1. **Upload**: PDF über Web-Oberfläche hochladen (Drag & Drop oder Dateiauswahl)
2. **Automatische Analyse**: das zuständige Importmodul erkennt den Beleg selbst
   - IBAN zur Kontoidentifikation
   - Einzelne Transaktionen (Datum, Empfänger, Betrag, fremde IBAN)
   - Plausibilitätsprüfung markiert Auffälligkeiten
3. **Organisation**: Dateien werden nach `./data/Belege/YYYY/Konten/<BANK>/` einsortiert
4. **Bestätigung**: Erkannte Transaktionen auf `/confirm_transactions` prüfen
5. **Import**: Nach Bestätigung als `BookingType='bank'` in Bookings-Tabelle

**Duplikat-Erkennung (zählbasiert):** Konto + Buchungsdatum + Betrag, ohne Text –
CAMT und PDF schreiben denselben Umsatz verschieden, und der WISO-Tabellen-Export
überschreibt ihn nachträglich. Gezählt wird **je Datei**: Der Bestand je Schlüssel
enthält auch, was frühere Dateien desselben Laufs angelegt haben; über mehrere
Dateien gilt das **Maximum**, nicht die Summe. „Alle importieren“ verhält sich damit
wie die Einzelimporte nacheinander: derselbe Auszug zweimal im Upload, CAMT und PDF
desselben Auszugs oder ein Tages- und ein Monatsauszug verdoppeln nichts, gleiche
Umsätze innerhalb *einer* Datei bleiben erhalten. Die Vorschau zeigt, was schon in
einer anderen Datei desselben Uploads steckt („in anderer Datei dieses Uploads“), die
Meldung nach dem Import nennt die so übersprungenen Umsätze. Passt der Verwendungszweck
eines solchen Umsatzes zu keinem Umsatz der früheren Dateien (gleich, Anfang des anderen
oder leer – Leerraum, Groß-/Kleinschreibung und ein führendes „Storno:“ zählen nicht), kommt in der Vorschau
„Text weicht ab, bitte prüfen“ dazu, etwa bei zwei verschiedenen Abos mit demselben
Betrag am selben Tag; das ist nur ein Hinweis, der Import bleibt beim Maximum.

Ein Umsatz **ohne Datum oder Betrag** legt der Import nie an: Die Vorschau zählt ihn
weder als neu noch als Duplikat („ohne Datum/Betrag – wird nicht importiert“), die
Meldung nach dem Import nennt die so übersprungenen Umsätze.

Grenzen (ohne Bankreferenz im Datenmodell nicht lösbar, siehe `docs/aufgaben.md`):
- Eine CAMT-Sammelbuchung wird in Einzelumsätze zerlegt, die PDF zeigt eine
  Summenzeile: Die Beträge passen nicht zusammen, beides wird angelegt.
- Zwei *verschiedene* Auszüge desselben Tags mit je einem echten, gleichen Umsatz:
  das Maximum behält nur einen davon. Die Vorschau markiert abweichende Texte, nachtragen
  geht nur von Hand – eine Option „trotzdem importieren“ fehlt (`docs/aufgaben.md`).
- Das VBR-PDF nennt das Jahr nur im Kopf des Auszugs und gilt für alle Zeilen; bei
  einem Auszug über den Jahreswechsel kann das Datum einzelner Zeilen abweichen, und
  derselbe Umsatz aus CAMT wird dann nicht als Duplikat erkannt.
- Die Vorschau wird beim Hochladen berechnet und nach einem Einzelimport nicht neu
  geladen: Wer zuerst die *spätere* Datei einzeln importiert, sieht für die frühere
  Karte eine zu hohe „neu“-Zahl. Das Ergebnis des Imports stimmt trotzdem.

### CAMT.053 (ISO 20022, XML)

`importers/camt.py` liest Kontoauszüge im Bankformat CAMT.053 – für alle Banken,
die es anbieten (camt.053.001.02/.04/.08, Namespace-unabhängig über lokale
Elementnamen). Upload von `.xml` oder `.zip` (mehrere XML-Dateien) läuft über
denselben Weg wie PDF: `BankStatement` → Vorschau → Bestätigung.

* Je `<Stmt>` ein Beleg; Konto-Zuordnung über `Stmt/Acct/Id/IBAN`, ohne Treffer
  „Konto nicht gefunden“ wie bei PDF.
* Nur gebuchte Umsätze (`Sts` = `BOOK`); Vorzeichen aus `CdtDbtInd`. Storno
  (`RvslInd`) wird mit Präfix „Storno: “ im Verwendungszweck übernommen.
* Sammelbuchung: mehrere `TxDtls` mit eigenem Betrag werden Einzelumsätze, sofern
  die Summe stimmt (sonst ein Umsatz + Hinweis `split_mismatch`).
* Gegenpartei: Dbtr bei Gutschrift, Cdtr bei Lastschrift; Zweck = alle `Ustrd`
  (Fallback `AddtlTxInf`, `AddtlNtryInf`). Valuta, EndToEndId, Mandats- und
  Gläubiger-ID hat das gemeinsame Datenmodell nicht.
* Plausibilität: Anfangssaldo (OPBD/PRCD) + Umsätze = Schlusssaldo (CLBD), sonst
  Hinweis `balance` in der Vorschau.
* Sicherheit: DTD/Entities werden abgelehnt, ZIP nur im Speicher mit Grenzen
  für Anzahl/Größe/Kompressionsverhältnis. Beträge nur als `Decimal`.

### Erweiterung für weitere Banken

In `document_parser.py` eine neue Methode hinzufügen:

```python
def parse_bank_statement_sparkasse(self, filepath: str) -> Dict:
    result = {'iban': None, 'document_date': None,
              'transactions': [], 'bank_code': 'Sparkasse'}
    # Parser-Logik
    return result
```

Erkennung in `parse_document()`:
```python
elif 'sparkasse' in text.lower():
    return self.parse_bank_statement_sparkasse(filepath)
```

---

## 2. WISO Mein Büro CSV-Import

### Übersicht

Zwei Export-Formate mit **automatischer Format-Erkennung**:

1. **Original-Export** (9 Spalten) → `BookingType='entry'`
2. **Tabellen-Export** (6 Spalten) → `BookingType='bank'`

Nach dem Import wird automatisch `link_bank_to_entries()` aufgerufen, das Bank- und Entry-Buchungen über `ParentBooking_ID` verknüpft.

---

### Format 1: Original-Export

**CSV-Format:**
```
ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
```

**Automatisches Mapping:**
- **KONTO** → `ChartOfAccounts.AccountNumber` → `COA_ID` (Sollkonto)
- **GEGENKONTO** → `ChartOfAccounts.AccountNumber` → `CounterCOA_ID` (Habenkonto)
- **SCHLUESSEL** → Steuersatz-Lookup über `TaxKeys`-Tabelle (50 BU-Schlüssel)
  - z.B. 9/401 → 19%, 8/402 → 7%, 121 → 0%, 490 → 0%
- **TaxAmount**: Automatisch berechnet als `|Brutto| - |Brutto| / (1 + TaxRate)`
- **Spezialfall 4405→4400**: Wenn kein BU-Schlüssel vorhanden ist,
    wird automatisch 19% gesetzt und TaxAmount berechnet

**Liquiditäts-Erkennung (`_is_liquid()`):**
- Bankkonten und die Kasse der App: Über `Accounts.SKRAccount` JOIN mit ChartOfAccounts
  (die Standard-Kasse trägt SKR04 1600; hat die Kasse keine SKR-Nummer, ist 1600 nicht liquide)
- Verrechnungskonto: 1460 (Geldtransit)
- Der SKR03-Kassenbereich 1000–1099 zählt seit 2.1.0 nicht mehr (in SKR04 sind es Vorräte): Der
  Import meldet Zeilen darauf (`stock_range_accounts`: Konto, Bezeichnung, Anzahl) mit dem Hinweis,
  dass ältere Versionen sie als Kasse gelesen haben und dass Buchungen gegen diese Konten als
  Umbuchung gelesen werden und nicht in der EÜR erscheinen
- Dieselbe Menge (`importers.wiso_fdb.DEFAULT_LIQUID_ACCOUNTS` plus die eigenen Konten) nutzen der
  Datenbank-Import, die Buchungsmaske und alles, was eine Zeile ohne Form liest
- Bestimmt Vorzeichen der Buchung **und die Form** des Satzes (`Bookings.EntryForm`):
  Konto liquide → `liquid_first` (Betrag positiv), Gegenkonto liquide → `purpose_first` (Betrag
  negativ), beide → `mirror_coa` (der Rohbetrag der Datei bleibt, positiv = Konto im Soll), keine →
  `transfer` mit dem Rohbetrag (WISO meint „Konto im Soll“, auch bei leerem oder unbekanntem Gegenkonto;
  die EÜR liest so einen Satz als Umbuchung, ohne Vorsteuerausweis). Bei genau einer liquiden Seite
  wird der Betrag zum Betrag der Geldbewegung; ein **negativer Bruttobetrag** verliert dabei sein
  Vorzeichen oder behält es zufällig, und der Import zählt jede solche Zeile (`negative_gross`) –
  ein Storno oder eine Gutschrift wird so wie eine normale Buchung gelesen. Ohne liquide Seite oder
  mit zwei bleibt der Rohbetrag, und die Zeile zählt nicht

**Form beim Re-Import:** Kein Import ändert die Form einer vorhandenen Zeile. Findet der Textexport zu
einer Zeile keinen Treffer mit gleichem Betrag, aber einen Spiegel des Bestands (`mirror_legacy`) mit
gleichem Beleg, Datum, Konto, Gegenkonto und dem Betrag −gross, den die Datei nicht selbst beansprucht
(eine eigene Zeile mit diesem Betrag), ist die Zeile ein Duplikat: Kein zweiter Satz, der vorhandene
bleibt, wie er ist. Der Import zählt solche Spiegel – mit gleichem wie mit umgekehrtem Vorzeichen –
als `legacy_mirrors`; die Seite *Verschiedenes* nennt die Zahl („noch nach der alten Lesart
gespeichert“).

**Zeilen auf Konten 1000–1099 aus einer älteren Version:** Bis 2.0.x galten diese Konten als Kasse, die
Zeile stand mit dem Betrag dieser Regel im Bestand („6815 an 1050“ −10, „1050 an 1800“ +30); heute gibt
derselbe Import ihr einen anderen Betrag und fände sie nicht wieder. Berührt eine Zeile ein Konto in
1000–1099 und unterscheiden sich die Beträge der alten und der heutigen Regel, sucht die Duplikatsuche
zusätzlich mit dem Betrag der alten Regel: Gleiche Beleg-Nr. (auch leer), gleiches Datum, Konto und
Gegenkonto (ein im Kontenrahmen fehlendes Konto gilt auf beiden Seiten als gleich) – dann ist die Zeile
ein Duplikat. Jede vorhandene Zeile zählt höchstens einmal, und nicht die, die eine andere Zeile der
Datei mit ihrem eigenen Betrag beansprucht. Die Altzeile bleibt, wie sie ist (Form und Betrag); der
Import zählt sie als `legacy_stock_rows`, und die Seite *Verschiedenes* nennt die Zahl.

**Duplikat-Erkennung (zählbasiert):** REFERENZNUMMER + Datum + COA_ID + Betrag;
ohne Referenznummer: Datum + COA_ID + Betrag (nur Entry-Buchungen, ohne Text —
der Tabellen-Export überschreibt ihn nachträglich). Übersprungen werden je
Schlüssel nur so viele Zeilen, wie die DB bereits enthält — gleichartige
Split-Positionen (gleicher Betrag/Konto/Tag) werden dadurch korrekt importiert.

**Zusammenspiel mit dem Datenbank-Import (`db/wiso_fdb_import.py`):** Der Schlüssel
fragt nicht, woher eine Buchung stammt – was der Datenbank-Import schon angelegt hat,
überspringt der CSV-Import. Umgekehrt übernimmt der Datenbank-Import Buchungen des
CSV-Imports, statt eine zweite anzulegen – aber eng gefasst, lieber eine sichtbare zweite
Buchung als eine still falsch übernommene:
- Kandidat ist eine `entry` ohne `SourceGroup`, ohne `AutoMirror` (Spiegelbuchung der
  Buchungsmaske) und ohne `Account_ID` mit gleichem Datum, Betrag, Konto und gleicher
  Beleg-Nr. (Leerraum zusammengefasst: „RE  12“ = „RE 12“; auch ohne Beleg-Nr.).
- Der **Text** muss immer gleich sein (Leerraum und Groß-/Kleinschreibung egal; die Datenbank
  liefert ihn als latin-1, die CSV als cp1252 – für den Vergleich wird er umgesetzt, gespeichert
  bleibt der Text der CSV-Zeile) und darf nicht leer sein – zwei leere Texte beweisen nichts,
  solche Sätze werden neu angelegt.
- Das **Gegenkonto** muss gleich sein (beide bekannt) oder der CSV-Zeile fehlen; ein anderes
  passt nie. Unter mehreren gewinnt das gleiche Gegenkonto, dann die kleinste ID. Jede Zeile wird
  höchstens einmal übernommen: zwei gleich hohe Teilbuchungen übernehmen zwei CSV-Zeilen, gleich
  hohe Zahlungen verschiedener Kunden mit verschiedenem Text bekommen jeweils ihr Gegenkonto.
- **Mehrdeutig** ist ein Schlüssel, wenn Sätze des Imports mit gleichem Datum, Betrag, Konto,
  Beleg-Nr. und Text verschiedene Gegenkonten tragen: Dann nehmen sie keine Zeile ohne
  Gegenkonto (welcher Satz zu welcher Zeile gehört, ließe sich nur raten); Zeilen mit gleichem
  Gegenkonto bleiben erlaubt.
- Fehlte der CSV-Zeile das Konto (im Kontenrahmen unbekannt), sucht der Datenbank-Import
  ersatzweise ohne Konto – nur wenn es zum Schlüssel keine Zeile mit Konto gab, und nur mit
  gleichem Gegenkonto und gleichem, nicht leerem Text.
- Ein Spiegel des Bestands (`mirror_legacy`) mit **umgekehrtem Vorzeichen** (Betrag −gross: Richtung
  auf dem Gegenkonto) wird übernommen, wenn Konto, Gegenkonto (beide bekannt), Beleg-Nr. und Text
  gleich sind – und die Datenbank nicht selbst eine Zeile mit seinem Betrag enthält. Form und Betrag
  bleiben (`legacy_mirrors` zählt ihn).
- Ein Satz, der ein Konto 1000–1099 berührt, wird zusätzlich mit dem Betrag der alten Regel gesucht
  (eine ältere Version hat die CSV-Zeile so gespeichert, siehe oben): dieselben Schutzregeln, und die
  Datenbank darf keinen Satz mit diesem Betrag selbst beanspruchen. Übernommen wird nur mit der
  Quellgruppe – Betrag, Form, Konten und Steuer der Altzeile bleiben (`legacy_stock_rows`). Eine Altzeile,
  deren Konto im Kontenrahmen fehlte (`COA_ID` leer), findet diese Suche nicht: Dann entsteht eine zweite
  Buchung (Grenze, `docs/aufgaben.md`).
- Übernommen wird mit einem UPDATE (im Replay-Log): `SourceGroup` (WISO-`ACCOUNTINGID`)
  setzen, leere Konten (`COA_ID`, `CounterCOA_ID`) und einen leeren Empfänger füllen,
  Steuersatz und -betrag ergänzen, wenn beide fehlen. Die Form: Ein `mirror_legacy`-Spiegel bleibt einer;
  füllt die Übernahme ein fehlendes Konto, ändern sich die Konten des Satzes, und für ihn gilt die Form
  des Imports. Text, Verknüpfungen zur Bank und
  Änderungen des Nutzers bleiben unberührt. Die Seite meldet die Zahl („aus dem CSV-Import
  wiedererkannt“) und listet die ersten 500 übernommenen Buchungen auf.
- **Möglicherweise doppelt:** Hat der Import zu Datum, Betrag und Konto einen Satz neu angelegt,
  meldet er die dazu vorhandenen, nicht übernommenen Buchungen (Zähler und die ersten 500 auf der
  Seite *Verschiedenes*, ohne Rücksicht auf Text und Beleg-Nr.). Stimmt es, ist von beiden eine
  zu löschen.

**Re-Import der Datenbank:** Eine schon bekannte `SourceGroup` wird nicht mehr nur übersprungen: Der
Satz wird mit der Zeile der Gruppe verglichen (Datum, Konto, Gegenkonto, |Betrag|). Ein
`mirror_legacy`-Spiegel wird gezählt (`legacy_mirrors`) und bleibt, wie er ist. Weicht eine andere
gespeicherte Form von der des frischen Imports ab (z. B. weil ein Konto inzwischen ein Geldkonto ist),
wird das nur gemeldet (`form_conflicts`, die ersten 500 auf der Seite *Verschiedenes*) – nichts wird
umgedeutet.

Grenze: Eine von Hand erfasste `entry` wird nur übernommen, wenn auch der Text stimmt. Eine
CSV-Zeile, deren Text, Gegenkonto oder Betrag nachträglich geändert wurde, wird nicht
wiedererkannt – es entsteht eine zweite Buchung, die unter „möglicherweise doppelt“ steht und
sich in der Buchungsliste löschen lässt. Die WISO-Satz-Id als eigene Spalte würde das exakt
machen (Schemaänderung, siehe `docs/aufgaben.md`).

**BookingType:** `'entry'` (Buchungssatz)

---

### Format 2: Tabellen-Export

**Zweck:** Bankbewegungen mit Empfänger und Verwendungszweck importieren

**CSV-Format (6 Spalten, Standard):**
```
Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr./opt. Beleg Nr.;Betrag
```

**CSV-Format (7 Spalten, erweiterter Export mit IBAN):**
```
Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag
```

Beide Varianten werden automatisch erkannt (Format-Erkennung über `Empf./Auft.` + `Verwendungszweck`).

**Vorbereitung:**
1. Tabellen-Ansicht in WISO für ein Bank-/Verrechnungskonto öffnen
2. Als XLS exportieren → in Calc öffnen
3. Spalte 1 (Status) und letzte Spalte (Saldo) löschen
4. Als CSV mit Semikolon speichern

**Automatisches Mapping:**
- **Empf./Auft.** → `RecipientClient`
- **Verwendungszweck** → `Text` (Zeilenumbrüche → Leerzeichen)
- **Kategorie** → `COA_ID` (automatisches Matching über SKR-Beschreibung)
- **Beleg Nr. / opt. Beleg Nr.** → `DocumentNumber`
- **Konto-Nr. / IBAN** → `ForeignBankAccount` (optionale Spalte, falls vorhanden)

**Matching-Logik (mehrstufig):**

Bei **Buchungen mit Belegnummer** (`Beleg Nr.` gesetzt):
- Stufe 1 (direkt): Datum + Belegnummer + Betrag → genau ein Treffer
- Stufe 2 (Bank-Parent): Bankbuchung mit gleichem Datum/Betrag suchen, Entry-Kinder per Belegnummer ergänzen
- Stufe 3 (Beleg-Split): Summenabgleich über alle Buchungen gleicher Belegnummer

Bei **Buchungen ohne Belegnummer** (`Beleg Nr.` leer):
- Stufe A (bank+entry-Paar): Wenn Datum+Betrag genau eine bank- und eine entry-Buchung treffen und die entry-Buchung Child der bank-Buchung ist → beide gemeinsam aktualisieren (Realfall: Privatentnahmen, Bankgebühren, Zinsen)
- Stufe B (Text-Disambiguierung): Verwendungszweck-Normalisierung als Tiebreaker

Gefunden → UPDATE (RecipientClient, ForeignBankAccount, COA_ID ergänzen — nur leere Felder)  
Nicht gefunden → in `not_found`-Liste

**BookingType:** `'bank'` (Bankbewegung)

---

### Format-Erkennung

Automatisch anhand der Spaltenüberschriften:
- **Original**: erkennt "KONTO" und "GEGENKONTO"
- **Tabelle**: erkennt "Empf./Auft." und "Verwendungszweck"

### Bank↔Entry-Verknüpfung (link_bank_to_entries)

Nach jedem WISO-Import wird `link_bank_to_entries()` aufgerufen:
- Mehrstufiges Matching:
    - Stufe 1: Datum + normalisierter Empfänger + Betrag
    - Stufe 2: Datum + Betrag (eindeutig nach Doppik-Filter)
    - Stufe 3: Split-Gruppen mit Summenabgleich (nur gleicher Tag)
    - Stufe 3b: Rechnungs-Split (SUM/Anzahl, Bank-COA als Marker)
    - Stufe 3c: Privatanteil-Split (Summe minus Privatentnahme-Offset)
    - Stufe 3d: Sammelzahlung (mehrere Rechnungsnummern im Bank-Text)
    - Stufe 4: DocumentNumber-Tiebreaker bei Mehrdeutigkeit
    - Stufe 5: Text-Token-Matching (lange Ziffernfolgen ≥ 8 Stellen)
    - Stufe 6: Text-Similarity ohne Belegnummer (`SequenceMatcher`)
    - Stufe 7: Debitoren-Auflösung (`Status='resolved'` für Debitoren-Entries, deren Zahlung bereits verknüpft ist)
- Doppik-Filter: Entry-Buchungen auf SKR-Bankkonten (z.B. 1810) werden ignoriert
- Forderungsbuchungen (Debitoren 10000 an Wartekonto, entstehen beim Versenden einer Rechnung) werden in keiner Stufe verknüpft – sie gehören zur Rechnung, nicht zur Bankbewegung (Ausnahme: die Quellklammer aus dem WISO-Datenbankimport). Die WISO-Zahlungssätze „Bank an 10000“ bleiben verknüpfbar
- Ergebnis: `Entry.ParentBooking_ID → Bank.ID`

### Encoding

Automatische Erkennung: CP1252 (Standard), UTF-8-SIG, UTF-8, Latin-1

---

### Verwendung über Web-Interface

1. Navigieren zu `/miscellaneous`
2. "WISO Import" → CSV-Datei auswählen
3. Ergebnis: Anzahl importiert / aktualisiert / übersprungen / verknüpft

### Programmatische Verwendung

```python
from db import Database

db = Database()

with open('export_wiso.csv', 'rb') as f:
    csv_bytes = f.read()

result = db.import_wiso_csv(csv_bytes)
print(f"Importiert: {result['imported']}")
print(f"Aktualisiert: {result['updated']}")
print(f"Übersprungen: {result['skipped']}")
print(f"Fehler: {result['errors']}")

# Danach: Bank↔Entry verknüpfen
linked = db.link_bank_to_entries()
print(f"Verknüpft: {linked}")
```

### Empfohlener Import-Workflow

1. **SKR-Konten** prüfen (alle verwendeten müssen vorhanden sein)
2. **WISO Original-Export** importieren → Entry-Buchungen mit TaxRate + TaxAmount
3. **WISO Tabellen-Export** importieren → Bank-Buchungen mit Empfänger + Verwendungszweck
4. Automatische Verknüpfung wird nach jedem Import durchgeführt
5. Ergebnis unter `/transactions` prüfen (verknüpfte vs. offene Bankbuchungen)
