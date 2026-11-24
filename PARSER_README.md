# Leser: Kontoauszüge, WISO-Importe und Belegerkennung

> WISO, WISO Mein Büro, DATEV und die genannten Banken sind Marken ihrer jeweiligen Inhaber.
> uBuchhaltung ist ein unabhängiges Projekt und steht in keiner Verbindung zu den
> Markeninhabern oder den Herstellern. Die Namen dienen allein der Bezeichnung der
> unterstützten Import- und Exportformate.

Diese Datei beschreibt den Aufbau der Leser und die Formate, die sie lesen. Die Regeln dahinter stehen in
`docs/regeln/import.md` (Kontoauszug-Import, WISO-Übernahme) und `docs/regeln/belege.md` (Belegerkennung); die
Schutzregeln und Grenzen dort gelten, sie werden hier nicht wiederholt. Offene Punkte: `docs/aufgaben.md`.

Abhängigkeiten (`pip install -r requirements.txt`): `pdfplumber` liest PDF-Kontoauszüge und den Text der Belege,
`fonttools` erzeugt PDF. Eine Texterkennung (OCR) für gescannte PDFs gibt es nicht.

## 1. Kontoauszüge (PDF, CAMT.053, CSV)

### Aufbau

Die Anwendung kennt nur die Abstraktionsschicht; die Eigenheiten einer Bank stecken in je einem Modul unter `importers/`.

| Datei | Aufgabe |
|---|---|
| `base.py` | Datenmodell: `BankStatement` mit `StatementTransaction`-Zeilen, Basisklasse `StatementImporter` |
| `pdftext.py` | Helfer für alle Module: Text, IBAN, Belegdatum, `find_balances()` |
| `plausibility.py` | Zentrale Inhaltsprüfung: `check_statement()`, `check_transaction()`, `check_balance()` |
| `vbr.py`, `dkb.py` | Volksbank Rottweil und DKB als PDF (Registry `IMPORTERS`) |
| `camt.py` | CAMT.053 (XML oder ZIP), nur im Speicher: `parse_upload()`, `parse_xml()` |
| `dkb_csv.py` | DKB-Umsatzliste als CSV, nur im Speicher: `parse_csv()` |
| `safe_xml.py` | XML aus Uploads sicher lesen (`parse_untrusted_xml`); gemeinsam mit der Belegerkennung |
| `__init__.py` | `IMPORTERS` (Erkennungsreihenfolge, das erste passende Modul gewinnt), `find_importer()`, `parse_statement()` |

Eine importierte Bewegung hat nur `CANONICAL_FIELDS`: `date`, `amount`, `recipient`, `reference`, `foreign_iban`. Weiter
reicht das Modell nicht (Valuta, EndToEndId, Mandats- und Gläubiger-ID fehlen).

**Neue Bank (PDF) anbinden:** ein Modul mit einer `StatementImporter`-Klasse schreiben (`detect(filename, text)` erkennt
den Beleg, `parse(filepath, text=None)` liefert den `BankStatement`) und die Klasse in `IMPORTERS` eintragen. Am übrigen
Code ändert sich nichts; `parse_statement()` prüft das Ergebnis danach mit `check_statement()`. Ein Dateiformat, das
nicht über den Text läuft (CAMT, CSV), liest der Upload-Handler selbst (`server/upload_handler.py`) und prüft es mit
`check_statement()`.

### Ablauf

1. Upload auf der Seite *Buchungen* (`POST /upload_receipts`): PDF, CAMT.053 (`.xml`, `.zip`) oder CSV (DKB-Umsatzliste).
2. Das zuständige Modul erkennt den Beleg selbst und liefert IBAN (Kontozuordnung) und Umsätze; die Plausibilitätsprüfung
   vermerkt Auffälligkeiten.
3. PDF-Auszüge legt `document_parser.py` unter `<Datenordner>/Belege/<Jahr>/Konten/<Bank>/` ab; CAMT und CSV werden nur im
   Speicher gelesen.
4. Die Vorschau (`server/import_preview.py`) zeigt neu, Duplikat und unvollständig; `POST /confirm_transactions` legt
   `BookingType='bank'` an. Duplikate, Auswahl einzelner Umsätze und die Grenzen: `docs/regeln/import.md`.

### Plausibilitätsprüfung

PDF-Text ist ungenau (Kopf- und Fußzeilen rutschen in Felder, Beträge fehlen, Jahre verrutschen). Die Befunde blockieren
den Import nie; die Vorschau zeigt sie, der Nutzer entscheidet. Die `boiler`-Muster greifen nur auf das **ganze** Feld
(„Blatt 3“ ja, „Blattwerk Gartenbau“ nein).

| Befund an einer Bewegung | Bedeutung |
|---|---|
| `amount` | Betrag fehlt (0,00 € ist ein gewöhnlicher Umsatz) |
| `date` | Datum fehlt oder ist unlesbar |
| `daterange` | Datum mehr als 400 Tage vor oder mehr als 31 Tage nach dem Belegdatum (Jahresdreher) |
| `empty` | weder Empfänger noch Verwendungszweck |
| `boiler` | Inhalt ist ganz Kopf-/Fußzeilentext („Seite 2 von 5“, „Neuer Kontostand“, Formularnummern) |
| `huge` | Betrag über 1.000.000 (meist ein verrutschtes Trennzeichen) |

Am Beleg: `no_transactions`, `no_iban`, `parse_error`, `balance` (Anfangssaldo + Umsätze ≠ Schlusssaldo), `no_balance`
(Saldo nicht gefunden, Vollständigkeit nicht geprüft), `unparsed` (Buchungskopf ohne lesbaren Betrag), `no_year` (Jahr
des Auszugs nicht erkannt), `foreign_currency` (CAMT nicht in Euro), `skipped_status`, `split_mismatch` (CAMT) und
`bad_entry` (CAMT). Beträge liest jeder Parser über `money.parse_amount` (deutsch `1.234,56`; die PDF-Parser verlangen das
Komma), die DKB-CSV über `money.parse_grouped_amount`.

Saldoprüfung der PDF-Auszüge: `pdftext.find_balances()` sucht die Zeilen „alter Kontostand …“ und „neuer Kontostand …“
(Betrag in derselben Zeile, dahinter `S`/`H`, `+`/`-` oder `EUR`), `plausibility.check_balance()` rechnet in ganzen Cent.
Bestätigt ist das an Volksbank-Auszügen eines Kontos; findet die Suche die Zeilen nicht, steht `no_balance`.

### CAMT.053 (`camt.py`)

Unterstützt camt.053.001.02/.04/.08; gelesen wird über den lokalen Elementnamen, der Namespace spielt keine Rolle.

* Je `<Stmt>` ein Beleg; Kontozuordnung über `Stmt/Acct/Id/IBAN`.
* Nur gebuchte Umsätze (`Sts` = `BOOK`); Vorzeichen aus `CdtDbtInd`; ein Storno (`RvslInd`) bekommt „Storno: “ vor den
  Verwendungszweck.
* Sammelbuchung: mehrere `TxDtls` mit eigenem Betrag (`TxDtls/Amt`) werden Einzelumsätze, wenn die Summe stimmt (sonst ein
  Umsatz mit Hinweis `split_mismatch`).
* Gegenpartei: `Dbtr` bei Gutschrift, `Cdtr` bei Lastschrift; Zweck = alle `Ustrd` (Fallback `AddtlTxInf`,
  `AddtlNtryInf`).
* Saldoprüfung: Anfangssaldo (OPBD/PRCD) + Umsätze = Schlusssaldo (CLBD).
* Grenzen: je XML 20 MB (entpackt), höchstens 50 XML-Einträge je ZIP, 100 MB entpackt insgesamt, Kompressionsverhältnis
  höchstens 200, Beträge unter 10¹².

### DKB-Umsatzliste als CSV (`dkb_csv.py`)

Zwei Formate, erkannt an der Datei, nie konfiguriert:

* **Neu:** Vorspann `"Girokonto";"DE…"`, `"Zeitraum:"`, `"Kontostand vom …"`; Kopf `Buchungsdatum;Wertstellung;Status;
  Zahlungspflichtige*r;Zahlungsempfänger*in;Verwendungszweck;Umsatztyp;IBAN;Betrag (€);…`; Datum `TT.MM.JJ`; UTF-8 mit BOM;
  die Gegenseite steht in zwei Spalten (bei einer Belastung der Zahlungsempfänger, sonst der Zahlungspflichtige).
* **Alt:** Vorspann `"Kontonummer:";"DE…"`, `"Von:"`, `"Bis:"`, `"Kontostand vom …"`; Kopf `Buchungstag;Wertstellung;
  Buchungstext;Auftraggeber / Begünstigter;Verwendungszweck;Kontonummer;BLZ;Betrag (EUR);…`; Datum `TT.MM.JJJJ`; oft
  Windows-1252; Varianten mit Komma statt Semikolon und mit verlorenem Umlaut in „Begünstigter“; die Gegenseite steht in
  einer Spalte.

Kopfzeile = erste Zeile, deren erste Zelle `Buchungsdatum` oder `Buchungstag` heißt; Spalten werden nach Bedeutung gefunden
(`MEANS`, ein umbenannter Spaltenname ergänzt dort ein Wort). Kodierung: BOM → striktes UTF-8 → Windows-1252. Die IBAN des
Kontos steht im Vorspann; die Datei nennt keinen Anfangssaldo (`no_balance` steht immer am Beleg), `document_date` bleibt
leer. Status „Vorgemerkt …“ wird übergangen (`skipped_status`). Grenzen: 20 MB, 100 000 Zeilen, Kopfzeile in den ersten
100 Zeilen, 100 Spalten, 2000 Trennzeichen je Zeile. Zeilenregeln und Schutzregeln: `docs/regeln/import.md`.

## 2. WISO-Textexporte (CSV)

Zwei Exportformate, erkannt an der Kopfzeile (`Database.import_wiso_csv(csv_bytes)`):

| Format | Erkennung | `BookingType` |
|---|---|---|
| Original-Export (9 Spalten) | `KONTO` und `GEGENKONTO` | `entry` |
| Tabellen-Export (7 Spalten, ältere 6) | `Empf./Auft.` und `Verwendungszweck` | `bank` |

Ein DATEV-Buchungsstapel (erstes Feld `EXTF`/`DTVF`) wird erkannt und mit Klartext abgelehnt. Kodierung: BOM → strenges
UTF-8 → CP1252 (so schreibt WISO) → Latin-1; eine Datei mit BOM, die kein gültiges UTF-8 ist, wird abgelehnt. Das
Ergebnis ist ein dict `{imported, updated, skipped, errors, format, …}` (`format`: `original`, `table`, `datev`,
`unknown`; der Original-Export nennt zusätzlich Zähler wie `negative_gross`, `legacy_mirrors`, `unknown_tax_keys`).

### Original-Export

```
ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER
```

| Spalte | Wird zu |
|---|---|
| `DATUM` (`TT.MM.JJJJ`) | Buchungsdatum |
| `KONTO` / `GEGENKONTO` | `COA_ID` (Soll) / `CounterCOA_ID` (Haben), über `ChartOfAccounts.AccountNumber` |
| `REFERENZNUMMER` | Beleg-Nr. (`DocumentNumber`); gleiche Nummer + Datum gruppiert Split-Positionen |
| `BRUTTOBETRAG` | Betrag (`money.parse_amount`, Minor Units) |
| `SCHLUESSEL` | Steuersatz (BU-Schlüssel, siehe unten); ein Wert von 1 bis 9999 steht als `Bookings.DatevKey` am Satz |

* **Steuerschlüssel:** Zuerst gilt die Regel des Exports (`db/datev_keys.py`, `key_rate`): 401/101/121 Regelsatz, 402/102/122
  ermäßigt, 7/6/5/4 die Sätze 16 % bzw. 5 % (Vorsteuer 7/6, Umsatzsteuer 5/4); vom 2020-07-01 bis 2020-12-31 gelten für diese
  Schlüssel die vertauschten Sätze. Sonst nennt `TaxKeys` (53 Zeilen) den Satz; 490 und 100 haben keinen. Ein unbekannter
  Schlüssel wird nicht geraten (Zeile ohne Satz, `unknown_tax_keys`). Der Steuerbetrag wird aus dem Brutto gerechnet
  (`_included_tax`, eine Rundung auf den Cent). Ohne Schlüssel (leer oder 0) setzt „4405 an 4400“ 19 %.
* **Vorzeichen und Form (`EntryForm`):** Bestimmt wird beides daran, welche Seite ein Zahlungskonto ist (`_is_liquid`:
  eigene Bank- und Kassenkonten über `Accounts.SKRAccount`, die Kasse trägt SKR04 1600, dazu das Verrechnungskonto 1460
  aus `importers.wiso_fdb.DEFAULT_LIQUID_ACCOUNTS`). Der SKR03-Kassenbereich 1000–1099 zählt nicht; Zeilen darauf meldet
  der Import (`stock_range_accounts`). Die Regeln stehen in `docs/regeln/import.md` (*Vorzeichen und Form*).

### Tabellen-Export

```
Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;opt. Beleg Nr.;Betrag
Buchungsdatum;Empf./Auft.;Verwendungszweck;Kategorie;Beleg Nr.;Betrag            (ältere Exporte, ohne IBAN)
```

Die Beleg-Spalte heißt `opt. Beleg Nr.` oder `Beleg Nr.`. Vorbereitung in WISO: Tabellenansicht eines Bank- oder
Verrechnungskontos als XLS exportieren, in einer Tabellenkalkulation die Statusspalte vorn und die Saldospalte hinten
löschen, als CSV mit Semikolon speichern.

Der Export legt nichts an, sondern **ergänzt vorhandene Buchungen** (nur leere Felder): `Empf./Auft.` → `RecipientClient`,
`Verwendungszweck` → `Text` (Zeilenumbrüche werden Leerzeichen), `Kategorie` → `COA_ID` (über den Kontonamen),
`Konto-Nr. / IBAN` → `ForeignBankAccount`. Die Zeile wird in dieser Reihenfolge gesucht; was nichts findet, steht in
`not_found`:

* Mit Beleg-Nr.: (1) Datum + Beleg-Nr. + |Betrag| genau ein Treffer; (2) Bankbuchung gleichen Datums und Betrags, deren
  Kinder die Beleg-Nr. tragen; (3) Beleg-Split: alle Buchungen gleicher Beleg-Nr. mit passender Summe.
* Ohne Beleg-Nr.: (A) Datum + |Betrag| trifft genau ein Bank-/Satz-Paar (Satz ist Kind der Bankbuchung, z. B. Entnahme,
  Gebühr, Zinsen); (B) der normalisierte Verwendungszweck entscheidet bei mehreren gleichartigen Buchungen.

### Verknüpfen (`link_bank_to_entries`)

Nach jedem WISO-Import ruft der Handler `Database.link_bank_to_entries()` auf (Rückgabe ein dict mit `linked`, `skipped`,
`repaired`, `resolved`, `errors`): Es setzt `ParentBooking_ID` der Sätze auf die Bankbewegung. Eine bestehende Verknüpfung
fasst es nie an. Die Stufen laufen in der Reihenfolge 0 (Quellklammer `SourceGroup`), 1 (Datum + Empfänger + Wirkung),
2 (Datum + Wirkung), 4 (Beleg-Nr. als Tiebreaker), 3/3b/3c/3d (Split-Gruppe, Rechnungs-Split, Privatanteil,
Sammelzahlung), 5 (Text-Token ab 8 Ziffern), 6/6b/6c (Text-Ähnlichkeit, mehrere Quellgruppen), 7 (Debitoren-Auflösung).
Entscheidend ist die Wirkung des Satzes auf das Konto der Bewegung mit Vorzeichen (`matching.link_effect`); Forderungsbuchungen
und Spiegel im Altformat werden nicht automatisch verknüpft.

### Programmatische Verwendung

```python
from db import Database

db = Database()          # ohne Benutzerkontext: ./data/buch.db

with open('export_wiso.csv', 'rb') as f:
    result = db.import_wiso_csv(f.read())
print(result['format'], result['imported'], result['updated'], result['skipped'], result['errors'])

link = db.link_bank_to_entries()      # die Web-Oberfläche tut das nach jedem Import selbst
print(link['linked'])
```

Empfohlene Reihenfolge: SKR-Konten prüfen (alle verwendeten müssen vorhanden sein), Original-Export importieren,
Tabellen-Export importieren, das Ergebnis auf der Seite *Buchungen* prüfen. Die Oberfläche steht auf der Seite *Daten*
(`/miscellaneous`).

## 3. WISO-Datenbank (Firebird-Dateien)

`db/wiso_fdb_import.py` (`Database.import_wiso_fdb(path, standard_chart_path=None, with_assets=True, with_invoices=True,
today=None)`) importiert über den Leser `importers/wiso_fdb/` die Mandantendatenbank `DB1.FDB`, optional mit dem Standard-
Kontenrahmen `DB0.FDB`. Gelesen wird ohne Firebird-Installation und nur lesend; die Dateien kommen als Upload
(`server/wiso_upload.py`, Formular auf der Seite *Daten*). Wiedererkennung, Re-Import, AfA und Grenzen: `docs/regeln/import.md`.

| Modul | Aufgabe |
|---|---|
| `importers/wiso_fdb/ods.py` | Datei- und Seitenformat; je Satz die jüngste abgeschlossene Version (TIP-Seiten, Differenzstrom `apply_diff`) |
| `importers/wiso_fdb/catalog.py` | Tabellen- und Spaltenkatalog der Datei |
| `importers/wiso_fdb/rtf.py` | RTF-Text der Rechnungspositionen |
| `importers/wiso_fdb/__init__.py` | `WisoDatabase`: Konten, Buchungen, Anlagen, Rechnungen; `read_wiso_database()` |

Gelesene Tabellen und Felder:

| Inhalt | Quelle |
|---|---|
| Kontenplan | `BAS_FINACC_PLAN` (SKR03/SKR04, Mandant vor Standard), `FINT_ACCOUNTS` (`BASEACCOUNTNO`), `FINT_EXTERNDEFS` (`EXTERNKIND` 82: gespeicherte SKR04-Nummer je Konto, nur Sachkonten unter 10000) |
| Buchungen | `MOV_FINACC_ACCRECORDS`; `ACCOUNTINGID` → `Bookings.SourceGroup`, `DATEVVATKEY` (optional) → `DatevKey` |
| AfA-Sätze | `INVENTORYID`, `INVENTORYAMORTID`, `ACCOUNTINGKIND` (28 = AfA), `BOOKINGKIND`, Plan in `MOV_INVENTORY_AMORTIZATIONS` |
| Anlagen | `BAS_INVENTORY` (u. a. `INVNO`, `PURCHASEAMOUNTNET`), Zahlungen in `MOV_INVENTORY_BOOKINGS` (positiv = Zahlung, negativ = Abgang) |
| DATEV-Nummern | `SYS_FINACCSYSTEMS` (Satz „Datev-Format“; nur `NAME`, `CONSULTANT_NO`, `CLIENT_NO`) |
| Rechnungen | Positionstext als RTF-Blob (`ARTDESCR`) an der Auftragsposition, auf die `ORDPOSID` zeigt; Einheiten als UN/ECE-Code aus `SUP_ARTICLES_UNITS.OPENTRANSCODE` |

Die SKR04-Nummer eines Kontos bestimmt der Leser in dieser Reihenfolge: (1) `BAS_FINACC_PLAN`, (2) die gespeicherte
Nummer aus `FINT_EXTERNDEFS`, (3) das Basiskonto, (4) die geprüften Nachträge `KNOWN_EQUIVALENTS`. Konten ohne Zuordnung
werden gemeldet, nicht geraten. Der Anschaffungswert einer Anlage (`WisoDatabase._purchase_price`) ist die Summe der
positiven Zahlungen plus der eingetragene Wert, wenn dieser mehr als 1,00 € beträgt (`PRICE_HELPER_LIMIT`) und nicht auf den
Cent der Zahlungssumme gleicht; Einzelheiten dort. Ein Fehler beim Lesen (`WisoDatabase.check`) bricht vor dem Schreiben ab.

### Bekannte Unterschiede zum Stapel, den WISO schreibt

Der DATEV-Export (`export/datev.py`) bildet den Stapel nach, den WISO Mein Büro schreibt, gemessen an einem Datenbestand;
eine Zusicherung, dass die Kanzlei ihn so annimmt, gibt es nicht. Unterschiede (Spaltennummern wie in `export/datev.py`, ab 0):

* Der Vorlauf trägt die Versionsnummer 700, WISO schreibt 510.
* Spalte 100 „Erlöskonto (Anzahlungen)“ bleibt leer, Spalte 105 „Skontosperre“ trägt „0“; WISO belegt die beiden umgekehrt.
* Der AfA-Satz des Erinnerungswerts nach dem Abgang einer Anlage wird nicht importiert (`memo_rows_skipped`) und fehlt im Stapel.
* Für ältere Jahre leitet WISO den BU-Schlüssel aus der heutigen Bedeutung neu ab; die App gibt den an der Buchung
  gespeicherten aus und leitet nur ab, wo keiner gespeichert ist.
* Das Soll/Haben-Kennzeichen folgt der Form des Satzes (`Bookings.EntryForm`).

## 4. Belegerkennung (`receipt_reading/`)

Das Paket liest die Felder einer hochgeladenen Belegdatei. Es bekommt Bytes, schreibt nichts und kennt weder `db/` noch
`server/`. Das Ergebnis ist eine **Lesung** (`Reading`): ein Vorschlag mit Herkunft, kein geprüfter Inhalt. Sie steht in
`DocumentReadings` (`DB_MODEL.md`). Gelesen wird auf Anfrage der Seite *Belege* (`POST /receipts/read`) in einem
Kindprozess mit hartem Zeitlimit; verknüpft wird nichts ohne Klick. Schutzregeln, Statuswerte und Ablauf:
`docs/regeln/belege.md` (*Belegerkennung*).

| Datei | Aufgabe |
|---|---|
| `__init__.py` | `Reading`, `read_document()`, Grenzen, `READER_VERSION`, Tabelle der Befund-Codes (im Kopf der Datei) |
| `detect.py` | Art der Datei: Endung UND Kennbytes müssen zusammenpassen (PDF, XML, Bild) |
| `einvoice.py` | E-Rechnung: UBL (`Invoice`, `CreditNote`) und CII, Felder nach EN 16931 |
| `pdf_embedded.py` | eingebettetes XML einer ZUGFeRD-/Factur-X-PDF finden, nur begrenzt entpackt |
| `pdf_text.py` | Text einer gewöhnlichen PDF (die ersten fünf und letzten drei Seiten) |
| `text_rules.py` | Textregeln: Datum, Rechnungsnummer, Absender, USt-IdNr., Brutto, Netto, Steuer, Währung, Art |
| `isolated.py`, `worker.py` | Lesen im Kindprozess (`python -m receipt_reading.worker`) |
| `importers/safe_xml.py` | XML sicher lesen (gemeinsam mit CAMT) |

* **Reihenfolge:** zuerst die E-Rechnung (hochgeladenes XML oder das eingebettete `factur-x.xml`, `zugferd-invoice.xml`,
  `xrechnung.xml` einer PDF), danach die Textregeln. Ein Bild oder eine PDF ohne Textebene ist `no_text`.
* **E-Rechnung:** BT-1 Rechnungsnummer, BT-2 Datum, BT-5 Währung, BT-27 Verkäufer, BT-31 USt-IdNr., BT-109 Netto, BT-110
  Steuer, BT-112 Brutto, Steuersatz aus BG-23 (nur wenn die Aufschlüsselung genau einen nennt) und die Art (Rechnung oder
  Gutschrift; eine Gutschrift trägt positive Beträge). Was nicht streng passt, bleibt leer mit Befund `bad_<Feld>`.
* **Textregeln:** Ein Betrag steht als `1.234,56` oder `119,00` in derselben Zeile HINTER seiner Bezeichnung
  (Rechnungsbetrag, Gesamtbetrag, Zahlbetrag, brutto, netto, Zwischensumme …, mit Rangfolge). Nicht gelesen werden `19,00 %`,
  Beträge mit unklarem Vorzeichen (`- € 119,00`, `(119,00)`, `119,00 CR`) und Leerzeichen als Tausendertrenner. Die Steuer
  steht HINTER dem Steuerwort; bei mehreren Beträgen gilt der, der als einziger zum Satz der Zeile passt. Die Währung gilt
  am Betrag (`€`/`EUR` Euro; `$`, `£`, `USD`, `CHF`, `GBP` machen ihn fremd). Mehrere verschiedene Werte für ein Feld
  bleiben leer (`conflict_<Feld>`); nur bei Brutto, Netto und Steuer entscheidet die Summenprobe (`by_checksum`). Jedes
  gelesene Feld nennt Zeile und Seite; die Proben (Netto + Steuer = Brutto, Netto × Satz = Steuer) stehen in `Raw`. Ein
  Kontoauszug ist kein Beleg (Befund `statement`).
* **Grenzen:** Dateien über 30 MB werden nicht gelesen, ein XML höchstens 20 MB (auch entpackt), aus einer PDF höchstens
  200 000 Zeichen Text (gespeichert werden 20 000), das Zeitlimit des Kindprozesses beträgt 15 Sekunden (Konstanten in
  `receipt_reading/__init__.py`). Die Regeln lesen nicht jede Rechnung; Gelesenes ist zu prüfen.
* **Anzeige:** Spalte „Gelesen“ der Belegliste und Kasten „Gelesen“ auf der Seite eines Belegs. Befunde erscheinen nur als
  feste Sätze (`receipt_reader.NOTE_TEXTS`); ein neuer Befund-Code braucht dort einen Satz. Ändern sich die Leseregeln,
  steigt `READER_VERSION`, ältere Lesungen gelten als ungelesen.
* **Vorschläge:** Zu einer gelesenen Datei (Status `ok`, Euro), die an keiner Buchung hängt, schlägt die Seite des Belegs
  bis zu fünf Bankbewegungen oder eigenständige Buchungen vor (Betrag gleich dem Brutto, Datum von 7 Tagen vor bis 90 Tage
  nach dem Belegdatum), geordnet nach Rechnungsnummer, gemeinsamen Namenswörtern und Nähe zum Datum; eine Rechnung erwartet
  einen Ausgang, eine Gutschrift einen Eingang (`db/receipt_matching.py`). Zugeordnet wird über „Belege verknüpfen“.
