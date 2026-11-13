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
2. **Tabellen-Export** (7 Spalten, ältere Exporte 6) → `BookingType='bank'`

Ein DATEV-Buchungsstapel (erstes Feld `EXTF`/`DTVF`) wird erkannt und mit Klartext abgelehnt; die
Meldungen der Formaterkennung nennen keinen Feldwert der Datei (der Vorlauf trägt Berater- und
Mandantennummer).

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
  wird der Betrag zum Betrag der Geldbewegung; diese Regel ist nach einer Messung an echten
  Daten festgeschrieben: WISO schreibt den Bruttobetrag nicht negativ (Konto = Soll, Umkehrungen wie
  Storno oder Erstattung stehen mit vertauschten Konten). Ein **negativer Bruttobetrag** fällt aus
  diesem Muster; der Import zählt jede solche Zeile (`negative_gross`) als Wächter. Ohne liquide Seite oder
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

**Unterkonten (Datenbank-Import):** `BAS_FINACC_PLAN` führt nur alte Kontenpläne; später angelegte
Konten (Unterkonten je Fahrzeug oder Projekt) fehlen dort. Die SKR04-Nummer jedes Kontos speichert WISO
in `FINT_EXTERNDEFS` (`EXTERNKIND` 82, Wert in `EXTERNVALUE`, je Konto `ACCOUNTNO` und Kontenplan
`FINTABLEID`; die höhere Plan-Nummer ist der jüngere Plan). Der Leser (`importers/wiso_fdb`) bestimmt die
SKR04-Nummer in dieser Reihenfolge: (1) `BAS_FINACC_PLAN` (Mandant vor Standardrahmen) – was dort eine
Nummer nennt, wird nie überschrieben; (2) für Konten ohne Nummer die gespeicherte aus `FINT_EXTERNDEFS`
(Mandant vor Standardrahmen, je Konto der jüngste Plan mit brauchbarem Wert); (3) nur für Konten ohne
gespeicherte Nummer der Rückgriff auf das Basiskonto (`FINT_ACCOUNTS.BASEACCOUNTNO`, die Kette wird
verfolgt – eine Näherung, eigene Unterkonten fallen dabei zusammen); (4) die geprüften Nachträge
(`KNOWN_EQUIVALENTS`). Grenzen der gespeicherten Nummer: Sie gilt nur für Sachkonten (`ACCOUNTNO` unter
10000, Wert eine ganze Zahl von 1 bis 9999, Text oder Zahl – alles andere wird übergangen), Personenkonten
bekommen nie einen Eintrag daraus, und ein Konto aus `KNOWN_EQUIVALENTS` nimmt an Schritt 2 nicht teil: Es behält
die Zuordnung von 2.2.4. Fehlt die Tabelle oder eine ihrer Spalten, entfällt Schritt 2 und der Leser
verhält sich wie bis 2.2.4. Fehlt das SKR04-Konto im Kontenrahmen der App, legt der Import es mit der
Bezeichnung des Unterkontos (sonst des Plans, sonst des Basiskontos) an (`created_coa`).
**Re-Import über einen Bestand aus einer älteren Version:** Dort stehen die Sätze auf dem Basiskonto. Der
Re-Import erkennt sie an der Quellgruppe (nicht am Konto), legt keinen zweiten Satz an und ändert Konto,
Betrag und Form nicht. Der Leser liefert dazu je Satz die Nummern nach der alten Regel
(`WisoBooking.legacy_account`/`legacy_counter_account`, nur wo sie abweichen); passt zu einem Satz einer
bekannten Gruppe keine Zeile mit den heutigen Konten, wohl aber eine mit den alten (Datum, |Betrag| und
Text gleich), zählt der Import sie (`subaccount_rows`, nur Fälle, in denen die alte Nummer die eines Basiskontos
war), und die Seite *Verschiedenes* nennt die Zahl. Grenze: Ein Satz ohne `ACCOUNTINGID` (leere Quellgruppe)
wird nur über die Übernahmesuche wiedererkannt, und die hängt am Konto – auf einem Unterkonto entstünde beim
Re-Import über einen älteren Import ein zweiter Satz. Die Übernahme CSV → Datenbank bleibt in ihren Regeln
unverändert: Die CSV trägt die eigenen Nummern, das Konto passt jetzt von selbst. Eine vorhandene Anlage
behält beim Re-Import ihr Anlagekonto (`Assets.COA_ID`).
**Unterkonten von Zahlungskonten:** Ist für einen Satz das Konto (oder Gegenkonto) nach der alten Regel ein
Zahlungskonto (Bank, Kasse, Verrechnungskonto), nach der neuen aber nicht – ein Bank-Unterkonto, dessen
gespeicherte Nummer in der App kein Zahlungskonto ist –, ändern sich Vorzeichen und Form: Der Satz würde als
Umbuchung gelesen und fiele aus der EÜR. Das wird gemeldet, nicht geraten (`liquid_subaccounts`: je neuer
Kontonummer `nummer`, `basis` – das Zahlungskonto, unter dem ein älterer Import sie las – und `anzahl`; höchstens 50;
Vorzeichen und Form folgen der neuen Nummer). Abhilfe: das Konto vor dem Import als Bank- oder Kassenkonto
anlegen und neu importieren.

**Anschaffungswert einer Anlage (Datenbank-Import):** Der Leser (`WisoDatabase._purchase_price`) nimmt die Summe der
**positiven** Zahlungen (`MOV_INVENTORY_BOOKINGS`; negative Zeilen sind der Abgang) **plus** den eingetragenen Wert
(`BAS_INVENTORY.PURCHASEAMOUNTNET`), wenn dieser zählt – gerechnet in Minor Units, nie als float-Summe. Er zählt nicht, wenn
er **bis 1,00 €** beträgt (`PRICE_HELPER_LIMIT`: ein Behelfsbetrag, den der Nutzer bei der Erstanlage einträgt), und nicht,
wenn er **auf den Cent gleich der Summe der Zahlungen** ist (dieselbe Anschaffung zweimal erfasst, von Hand und über die
Zahlungen: sie zählt einmal; die Warnung zum Restbuchwert bei einem Abgang, den WISO mit dem doppelten Wert bucht, bleibt).
Sonst sind es zwei echte Beträge (ein Gerät von Hand eingetragen, die Montage als Zahlung), und der Anschaffungswert ist ihre
Summe – dann steht in `asset_warnings`: „{Anlage}: Der Anschaffungswert ({Summe} €) setzt sich aus dem in WISO eingetragenen
Wert ({eingetragen} €) und den Zahlungen ({Zahlungen} €) zusammen – bitte prüfen, ob beides zur Anschaffung gehört.“ (ein
Wert, der der Zahlungssumme nur um einen Cent nicht gleicht, würde sonst still den Anschaffungswert fast verdoppeln). Ohne
Zahlungen gilt wie bisher `PURCHASEAMOUNTNET`, wenn es über dem Platzhalter 0,01 € (`PRICE_PLACEHOLDER`) liegt. Der
Buchwert eines Planjahrs (`_depreciations`) ist die Summe der bis zum Jahr geleisteten Zahlungen plus der zählende eingetragene
Wert – dieser trägt kein Datum und gilt ab dem Kaufjahr – minus die kumulierte AfA.

**Vorausgeplante AfA (Datenbank-Import):** WISO schreibt den ganzen AfA-Plan sofort als Buchungssätze in
`MOV_FINACC_ACCRECORDS` (`INVENTORYID` gesetzt), auch für künftige Jahre und ohne Kennzeichen „geplant“. Ob ein
Satz mit `INVENTORYID` ein **AfA-Satz** ist, entscheidet der Leser in dieser Reihenfolge (`_is_depreciation`):
(1) `INVENTORYAMORTID` gesetzt – der Verweis auf die AfA-Zeile; (2) WISOs Buchungsart `ACCOUNTINGKIND` = 28, auch bei
leerem `INVENTORYAMORTID` (an einer Datenbank einer WISO-Version gemessen: AfA-Sätze tragen Art 28 und `BOOKINGKIND` 41,
Abgangsbuchungen Art 31 und `BOOKINGKIND` 44/45; die Spalte ist optional, ihr Fehlen ist kein Blocker der Prüfung);
(3) der Satz hat die Spalte `INVENTORYAMORTID`, aber keinen Wert, und die Art ist nicht 28 – eine Abgangsbuchung am
Anlagegut, auch am Tag einer AfA-Zeile: kein AfA-Satz; (4) nur wenn der Satz die Spalte `INVENTORYAMORTID` nicht kennt – die Tabelle hat
sie nicht (andere WISO-Version), oder er steht in einem älteren Satzformat; `catalog.rows` liefert dafür keinen Schlüssel,
bei einer vorhandenen Spalte mit leerem Wert dagegen `None` – und die Art nicht 28 ist, gilt ersatzweise die AfA-Tabelle
(`MOV_INVENTORY_AMORTIZATIONS`): eine Zeile derselben Anlage mit demselben Datum (ISO) **und** demselben Betrag
(|`AMOUNTGROSS`| = |`AMORT_AMOUNT`|, auf den Cent). Ein Satz ohne `INVENTORYID` ist nie einer. Das Planjahr eines Satzes
(`WisoBooking.depreciation_year`) ist das Jahr der AfA-Zeile (über `INVENTORYAMORTID`), sonst das des Satzdatums.

Stichtag ist der Tag des Imports (Ortsdatum; `import_wiso_fdb(..., today=...)` für Tests). **Ein AfA-Satz mit einem Datum
nach dem Stichtag liefert der Leser gekennzeichnet** (`WisoBooking.future_depreciation`) und zählt für ihn nichts – keine
unbekannten Konten, keine negativen Bruttobeträge, keine Unterkonten von Zahlungskonten, und er steht nicht unter den
übersprungenen Sätzen (`tax_rows_skipped`, `memo_rows_skipped`); die Regel für den Erinnerungswert nach dem Abgangsjahr hat
Vorrang. **Der Import legt ihn nie an**, übernimmt ihn nie aus einer CSV-Zeile, legt keine Konten nur für ihn an
(`_create_missing_coa`), macht keinen Nachtrag und prüft keinen Konflikt mit der App (`future_depreciations_skipped`
zählt ihn). Einzige Ausnahme ist ein Bestand aus einem Import bis 2.2.4, der den künftigen Satz schon in die Bücher
geschrieben hat: Ist seine Quellgruppe bekannt und gleicht ihm eine Zeile der Gruppe (Datum, Konten, |Betrag|, wie bei jedem
Re-Import; auch auf dem Basiskonto eines Unterkontos) – oder liegt, vom Nutzer geändert, eine freie Zeile der Gruppe in
seinem Planjahr (siehe *Geänderter AfA-Satz*) –, bleibt die Zeile unverändert, sie zählt als gebucht
(`future_depreciations_kept`, auch in `skipped`), und die Seite *Verschiedenes* nennt die Zahl.

**Der Status eines Planjahrs in `AssetDepreciations` folgt der Buchung, nicht dem Datum:** `booked`, wenn der AfA-Satz der
Anlage für das Jahr in den Büchern steht (neu angelegt, aus dem CSV-Import übernommen oder als Zeile seiner bekannten Gruppe
wiedererkannt oder als geänderter Satz anerkannt – auch ein künftiger Satz aus einem Import bis 2.2.4), sonst `planned` (`depreciations_planned` zählt die
Planjahre des Laufs, die danach geplant sind): Die App bucht sie im jeweiligen Jahr selbst, das geschieht nicht
automatisch. Der Buchungsschritt führt den Status mit, in derselben Transaktion wie die Buchungen und auch bei
`with_assets=False`; die Planzeilen schreibt danach `_insert_wiso_assets` mit der Menge der Jahre, deren Satz in den
Büchern steht – ein Bestand ohne Plan (Import bis 2.2.4 ohne Anlagen) bekommt so alle Jahre als gebucht, auch wenn die
Anlage der App inzwischen gelöscht wurde. Ein bisher geplantes Jahr wird nur dann `booked`, wenn sein Satz in den Büchern
steht; ein anderer Status ändert sich nie (auch ein `booked` aus einem Import bis 2.2.4 bleibt).
**Ein gebuchtes Jahr trägt den Betrag der Bücher:** Der Buchungsschritt liefert `{(INVENTORYID, Jahr): Betrag}` – die
**Wirkung auf das Anlagekonto** (`db.matching.depreciation_effect`) der angelegten, aus dem CSV-Import übernommenen oder
wiedererkannten Zeile, bei einer vom Nutzer geänderten Zeile die der gespeicherten Zeile, bei mehreren AfA-Sätzen eines Jahres die
Summe: Eine Abschreibung („6222 an 520“) zählt +|Betrag|, eine Korrektur mit vertauschten Konten („520 an 6222“, Anlagekonto im
Soll) −|Betrag|; die Soll-Seite folgt der gespeicherten Form (bei Form `transfer`: Betrag > 0 – Konto im Soll, Betrag < 0 –
Gegenkonto im Soll); berührt keine Seite ein Anlagekonto (1–999) oder beide, zählt der Satz +|Betrag|. Die Summe eines Jahres ist
nie unter 0: Übersteigen die Korrekturen die Abschreibung, steht 0, und das Jahr bleibt gebucht. Eine Planzeile, die der Lauf auf
`booked` hebt (am Ende der Transaktion der Buchungen) oder neu als `booked` anlegt, bekommt den Betrag als `DepreciationAmount`,
nicht den Planwert aus WISO. Betrag und Buchwert einer vorhandenen Planzeile aktualisiert der Re-Import nur, solange sie `planned`
ist; eine Zeile `booked` bleibt – auch wenn WISO den Betrag inzwischen ändert oder den Satz nicht mehr liefert: Der Plan der App
nimmt die gebuchten Beträge als Geschichte, und die Summe der AfA in den Büchern (Abschreibungen minus Korrekturen) soll der
Anschaffungswert bleiben. Einzige Ausnahme: Steht der Satz in diesem Lauf in den Büchern und weicht der Betrag einer Zeile `booked`
(nicht von der App gebucht) davon ab – Bestand aus einem Import bis 2.2.5, der den Planwert aus WISO trägt –, gleicht der
Buchungsschritt sie an die Bücher an (nur bei Abweichung; im Replay-Log wie das Anheben, am Ende der Transaktion der Buchungen).
Steht der Satz nicht in den Büchern dieses Laufs, bleibt die Zeile, wie sie ist.

**Geänderter AfA-Satz:** Gleicht einem AfA-Satz einer bekannten Gruppe keine Zeile genau (Datum, Konten, |Betrag|), hat die
Gruppe aber noch eine freie Zeile mit einem Datum im Planjahr (`depreciation_year`; gleich welcher Betrag, welcher Tag,
welche Konten), hat der Nutzer den Satz geändert: Er steht in den Büchern, es wird nichts angelegt, und das Jahr gilt als
gebucht (ein geplantes Jahr der App wird `booked`, eine neue Planzeile ebenso). Die Reihenfolge ist fest: Erst bekommt jeder
Satz aller bekannten Gruppen seine genaue Zeile, jede Zeile gehört höchstens einem Satz; erst danach dienen die übrigen
Zeilen als Nachweis, und auch dann jede höchstens einem AfA-Satz. Ein Satz, dessen Zeile der Nutzer gelöscht hat, nimmt so
nie die Zeile eines anderen Satzes derselben Gruppe und desselben Jahres. Grenzen: Der Nachweis gehört keinem bestimmten
Satz (bei zwei Sätzen eines Jahres und nur einer verbliebenen, geänderten Zeile nimmt sie der erste der Datei), die
geänderte Zeile eines anderen, nicht erkannten Satzes derselben Gruppe im selben Jahr zählt ebenso (die Zeile trägt keinen
Verweis auf die Anlage), und ein Satz, den der Nutzer in ein anderes Jahr verschoben hat, ist nicht zu erkennen – sein
Planjahr zählt dann nicht als gebucht.

**Nachtrag:** Hat die Gruppe für das Planjahr keine Zeile (der Erstlauf hat nur die vergangenen Jahre angelegt, die Gruppe
trägt mehrere), wird der AfA-Satz nur dann angelegt, wenn die Anlage in der App eindeutig zugeordnet ist (über die
Inventarnummer) **und** ihre Planzeile dieses Jahres existiert, `planned` ist und nicht von der App gebucht. In jedem anderen
Fall – keine Anlage in der App, keine Planzeile, Planjahr `booked`, Anlage ohne eindeutige Inventarnummer, ein Lauf mit
`with_assets=False` ohne Bestand – wird ein Satz einer bekannten Gruppe nie neu angelegt (der Nutzer kann ihn gelöscht
haben). Hat die App ein Jahr schon selbst gebucht (`Booking_ID` zeigt auf eine
Buchung, `book_depreciation`), bleibt die Planzeile unverändert, und der AfA-Satz der Datenbank für dieses Jahr wird
nicht angelegt (`depreciation_conflicts`, die ersten 100 Fälle in `depreciation_conflict_rows`); löscht der Nutzer die
Buchung der App, gibt das Löschen das Jahr frei, und der nächste Import legt den Satz an. Die Anlage der App wird über die
Inventarnummer gefunden (`Assets.InventoryNumber` = `INVNO`), aber nur, wenn sie in der WISO-Datenbank eindeutig ist:
Anlagen ohne Inventarnummer oder mit einer mehrfach vergebenen nehmen am Abgleich nicht teil (für ihre AfA-Sätze gilt
der Stichtag und sonst die Gruppenlogik ohne Anlagenbezug) und stehen in `asset_warnings`. `book_depreciation` rechnet mit
dem Plan der App (`Database.depreciation_plan`) und überschreibt den Betrag der Planzeile: Die gebuchten Jahre stehen mit
ihrem gebuchten Betrag im Plan, der Restbuchwert (Anschaffungswert minus gebuchte Beträge) verteilt sich linear auf die
Monate bis zum Planende (Kaufmonat plus Nutzungsdauer), je Jahr kaufmännisch auf den Cent gerundet, das letzte Jahr
bekommt den Rest; nicht gebuchte Jahre zwischen gebuchten haben keine eigene Zeile, und ein Jahr, das auf 0 Cent rundet,
entfällt (die Summe bleibt der Rest). Nur eine Anlage, deren gebuchte Jahre alle
dem Plan der Formel (`calculate_depreciation_plan`: linear, degressiv, GWG) gleichen, behält diesen. Gebucht ist, was in den
Büchern steht: bei einem von der App gebuchten Jahr die Wirkung der Buchung auf das Anlagekonto (ändert der Nutzer die Buchung im
Editor, folgt der Plan; eine zur Rückbuchung umgedrehte Buchung zählt mit 0), bei einem importierten Jahr die Planzeile – ändert
der Nutzer eine **importierte** AfA-Buchung nachträglich, folgt der Plan nicht (keine Verknüpfung zwischen Planjahr und
importierter Buchung). Die Buchwerte der gebuchten Jahre rechnen fortlaufend vom
heutigen Anschaffungswert, und in der Fortschreibung bucht `book_depreciation` nur das erste offene Jahr (sonst würden die Jahre
davor zu Lücken). Nach dem Schreiben der Anlagen vergleicht der Import je Anlage mit geplanten Jahren die Pläne; weicht ein Jahr
um **mehr als 1,00 €** ab oder plant die App dafür keine AfA, steht eine Warnung in `asset_warnings` und zählt in
`depreciation_plan_mismatches` – beim Buchen gilt der Plan der App. Die Schwelle: WISOs Plan rechnet mit dem Behelfsbetrag
bis 1 €, den der Anschaffungswert der App nicht enthält; kleinere Abweichungen sind damit erklärt.
**Restbuchwert ohne WISO-Plan:** Hat eine **nicht abgegangene** Anlage nach dem Lauf kein geplantes Jahr mehr (alle Sätze aus
WISO stehen in den Büchern), plant die App aber noch ein offenes Jahr mit mehr als 1,00 € – etwa weil der Anschaffungswert größer
ist als die Basis von WISOs Plan –, steht in `asset_warnings`: „{Anlage}: Nach den gebuchten Jahren bleibt ein Restbuchwert von
{R} €, für den WISO keine AfA mehr plant – bitte den Anschaffungswert prüfen, bevor die AfA in der App gebucht wird.“
(`depreciation_rest_warnings` zählt sie). Eine abgegangene Anlage ist ausgenommen: Ihren Restbuchwert bucht der Abgang aus.

**Reihenfolge und Sperre:** Der Buchungsschritt läuft in EINER schreibenden Transaktion (`BEGIN IMMEDIATE`), die als
Erstes die Anlagen der App und ihre Planjahre liest (`_link_assets`) – zwischen Lesen und Schreiben kann keine andere
Anfrage ein Jahr buchen (`book_depreciation` braucht dieselbe Sperre) –, dann die Buchungen schreibt (samt Status vorhandener
Planjahre); danach folgen die Anlagen samt AfA-Plan in einer eigenen Transaktion. Scheitert der Schritt der Anlagen, stehen die
Buchungen und der Status der vorhandenen Planjahre schon fest; es fehlen nur die neuen Planzeilen und die Änderungen an den
Anlagen – ein neuer Lauf trägt sie nach. Bestand aus einem Import bis 2.2.4 (künftige AfA-Buchungen mit Quellgruppe, Jahre
`booked`) bleibt unberührt – mit jedem Stichtag. Der Textexport kennt keine Anlagenzuordnung und prüft das nicht.

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

Automatische Erkennung in dieser Reihenfolge: UTF-8 mit BOM, strenges UTF-8, CP1252 (so schreibt WISO),
Latin-1. CP1252 dekodiert fast jede Bytefolge und steht deshalb nie zuerst; eine Datei mit BOM, die
danach kein gültiges UTF-8 ist, wird abgelehnt.

### Steuerschlüssel

`SCHLUESSEL` wird über die Tabelle `TaxKeys` in einen Steuersatz übersetzt. Ein Schlüssel, den die
Tabelle nicht kennt, wird nicht geraten: Die Zeile kommt ohne Steuersatz und zählt in
`unknown_tax_keys` (Hinweis auf der Seite *Verschiedenes*).

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
