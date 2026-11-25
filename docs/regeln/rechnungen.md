# Rechnungen und Angebote: Dokumente, Steuer, Sperre, Storno, Nummern

## Dokumente (PDF + XRechnung)

- Ablage `<Datenordner>/invoices/<Jahr des Rechnungsdatums>/<Rechnungs-Nr.> <Kunde>.pdf`, XRechnung gleich mit `.xml`; Schema
  zentral in `export/invoice_files.py`.
- **Hybride E-Rechnung (ZUGFeRD/Factur-X, EN 16931):** jede neue Rechnungs-PDF ist PDF/A-3b mit eingebetteter `factur-x.xml` (CII,
  `/AFRelationship /Alternative`). Fluss: DB → `export/einvoice_model.py` (Minor Units, Aufschlüsselung als Liste) → `cii_invoice.py`
  (eingebettet), `xrechnung_invoice.py` (UBL, Download). PDF/A in `export/pdfa.py` für alle PDFs; nur Rechnungen bekommen XML.
- Schriften: Liberation Sans (OFL 1.1) aus `export/fonts/`, metrisch gleich Helvetica (Layout unverändert), als Untermenge per `fontTools`
  eingebettet, Content-Streams bleiben WinAnsi. Das sRGB-ICC für den OutputIntent erzeugt `tools/make_srgb_icc.py` (Normwerte, keine Fremdlizenz).
- Validierung: CI-Job `einvoice` prüft `tools/make_einvoice_samples.py`-Beispiele mit Mustang (Version und SHA-256 gepinnt, nur mit
  neuer Prüfsumme ändern); das Werkzeug bricht bei Lücken ab (Prüfung und Mustang müssen übereinstimmen).
- **Leistungsdatum ist Pflicht** (§ 14 UStG): `handle_invoice_save` lehnt ohne ab (nicht bei Angeboten, versendeten Rechnungen,
  Importen); Angebot → Rechnung setzt es auf das Rechnungsdatum.
- **Leistungszeitraum** (`Invoices.ServiceDateEnd`, NULL = nur das Leistungsdatum): Beginn ist das Leistungsdatum, das Ende steht in der Spalte. Einen Zeitraum gibt es nur mit zwei ISO-Daten und
  einem Ende NACH dem Beginn – gelesen an EINER Stelle (`db.invoices.service_period`, für PDF, E-Rechnung und DATEV), geprüft beim Speichern (`normalize_service_end`: Ende vor dem Beginn oder
  kein Datum = 400 mit festem Klartext, ein Ende am Beginn wird NULL); was die Datenbank sonst enthält (SQL-Konsole), gilt als „kein Zeitraum“. Das Feld „bis:“ der Maske steht unter dem
  Leistungsdatum. PDF-Kopf in zwei Zeilen: „Leistungszeitraum:“ mit dem Beginn, darunter „bis <Ende>“, ISO wie die übrigen Kopfdaten (eine Zeile „von – bis“ passte nicht in die Wertspalte).
  E-Rechnung: CII `BillingSpecifiedPeriod` und UBL `cac:InvoicePeriod` (BG-14: Beginn BT-73, Ende BT-74) statt des Lieferdatums – BT-72 entfällt dann; ohne Zeitraum bleibt alles, wie es war.
  DATEV: Spalte 114 = das Ende (`auswertungen.md`). Stornorechnung und Ersatzrechnung übernehmen ihn (`_copied_head`), „Als neu kopieren“ füllt das Feld; im Angebot gibt es keinen.
  `tests/test_service_period.py`
- **Zahlungsart** (`Invoices.PaymentMeans`, Code nach UNTDID 4461): NULL = Überweisung (58), `'10'` bar, `'48'` Karte, `'1'` nicht festgelegt – geschrieben über `stored_payment_means` (die 58 wird NULL,
  jeder andere Wert 400), gelesen streng über `payment_means_code` (alles andere ist nicht lesbar: nichts wird still zur Überweisung). Die Überweisung steht wie immer mit Code 58 und IBAN in CII und UBL
  (ohne IBAN kein Element), jeder andere Code als der Code allein, ohne Konto; eine nicht lesbare Zahlungsart schreibt nichts. Das PDF ändert sich nicht. Auswahl in der Maske im Fuß unter der Bankverbindung.
  Lastschrift (59) gibt es nicht. `tests/test_payment_means.py`
- Kopfblock im PDF folgt der Maske (`tests/test_pdf_header_block.py`); was fehlt, bekommt keine Zeile.
- **Dateinamen-Kollision:** gehört der Zielpfad der Datei eines anderen Belegs, bekommt der Name `(ID n)` (`invoice_file_path(…, db)`);
  der Rückfall über das Schema nimmt nie die Datei eines anderen Belegs. **Das Dokument zeigt nie den Stand der Zahlungen** (`prepaid` = 0).
- **Erzeugt** wird nur über „PDF + E-Rechnung“ und automatisch beim Wechsel Entwurf/abgeschlossen → versendet. Versendete und
  stornierte Rechnungen (`db.invoice_lock.FROZEN_STATUSES`) werden nicht neu erzeugt (`overwrite_sent` nur beim Eintritt in
  „versendet“); fehlt die Datei, darf sie erzeugt werden. Versendet ↔ überfällig erzeugt nichts.
- **Download** liefert immer die abgelegte Datei (`/invoice/pdf_download`, `/invoice/xml_download`), nie eine frisch erzeugte; fehlt
  sie: 404. Fassungen: `GET /invoice/version_download?id=&file=&year=` (lesend): `file` reiner Dateiname, nur `.pdf`/`.xml` in
  `Fassungen/<id>` genau dieser Rechnung (`path_in_documents`), `year` nur ASCII-Ziffern; alles andere 404. Browser: `appDownload()`.
- **Angebote:** `/quote/pdf_download` nur die abgelegte PDF (`quotes/<Jahr>/…`), sonst 404. Erzeugen nur per POST
  (`/invoice/pdf_generate`, `/quote/pdf_generate`; GET 405). `/worktime/pdf` schreibt nichts auf die Platte (`store=False`).
- **Belegtyp:** Rechnungen und Angebote teilen `Invoices`; jede Route prüft den Typ (`Database.get_document`,
  `invoices.document_type`, beim Schreiben `… AND DocumentType = …`); die ID der anderen Art ergibt 404 und ändert nichts.
- **Gespeicherte Dokumentpfade** (`PDFPath`/`XMLPath`) gelten nur im Dokumentordner des Benutzers (`invoices`/`quotes`;
  `path_in_documents`, `realpath`). Jede Stelle, die einen solchen Pfad öffnet oder löscht (Download, E-Mail-Anhang,
  `drop_stale_file`), geht über `stored_invoice_path`. Pfad außerhalb = nicht vorhanden, nur im Server-Log.

## Steuer je Satzgruppe

- **`tax_groups.rate_groups` ist die EINE Stelle** für die Steuer je Satz (rein: nur `money`; beliebig viele Sätze, nie „19 und 7“
  fest): Steuer je Gruppe aus der Netto-Summe, kaufmännisch auf den Cent (BR-CO-17), Belegsteuer = Summe, Satz absteigend. Rechnen
  darüber: `handlers.recompute_invoice_totals`, `einvoice_model.build_einvoice`.
- `rate_pct` = Regel von `einvoice_model.rate_to_pct` ohne Vorgabewert; `handlers._rate_to_pct` ist bewusst nicht davon abgeleitet.
- Tests: `tests/test_single_rate_snapshots.py` (`tests/single_rate_golden_2_6_6.json`: ändert sich ein Schnappschuss, ist der Code zu
  ändern, nie die Datei), `tests/test_tax_rate_readers.py` (eine neue Lesestelle schlägt an: prüfen, ob sie mit mehreren Sätzen
  umgeht), `tests/test_tax_groups.py`.

## Mehrere Steuersätze je Beleg (Rechnung und Angebot)

- **Kennzeichen** `Invoices.MixedTaxRates` (NULL = ein Satz, `1` = je Position) liest EINE Funktion streng:
  `db.invoices.has_mixed_rates` (nur die Zahl 1; in `db/invoice_payments.py`). Geschrieben nur NULL oder 1 (`insert_invoice`).
- `Invoices.TaxRate` ist bei `MixedTaxRates = 1` nur Platzhalter (höchster Satz als Bruch), nie negativ. Was rechnet oder bucht,
  nimmt die Satzgruppen, nie den Kopfsatz.
- **Speichern prüft EINE Funktion** (`handlers._tax_fields`; verglichen wird der ungerundete Satz, `_exact_rate`). `taxMode` fehlt/leer
  oder `'mixed'`, sonst ungültig. Reihenfolge:
  1. Ohne Steuerausweis (`showTax` falsch): `'mixed'` abgelehnt, sonst Kopf −1, jede Position Satz 0.
  2. Mit Steuerausweis ohne `taxMode`: Kopf 0–100 %, alle Positionen GENAU derselbe Satz wie der Kopf; Satz zwischen 0 und 1 % wird
     abgelehnt (nicht eindeutig lesbar).
  3. `'mixed'`: jede Position GENAU einen Satz aus `tax_groups.MIXED_TAX_RATES` (19, 7 %) oder einem wählbaren Stammdaten-Eintrag
     (`taxRateId`). Alle gleich = gewöhnlicher Beleg (Kennzeichen NULL), sonst Kopf = höchster Satz, Kennzeichen 1.
  - Ablehnungen: fester Klartext `TAX_*` in `handlers.py`, vor dem ersten Schreiben, keine Nummer verbraucht.
- Versendet: Satz einer Position gesperrt (`_items_differ`), Kennzeichen abgeleitet, nie für sich verglichen (`invoice_lock.FIELDS`).
  `convert_quote_to_invoice`, Stornorechnung und Ersatzrechnung (`_copied_head`) übernehmen es.
- **Gruppen:** `Database._mixed_groups` (`tax_groups.mixed_groups`; auf den Cent gleich den Summen, sonst `MIXED_GROUPS_MISMATCH`;
  Platzhalter, negative/unlesbare/über 100 liegende Sätze, nicht ganzzahlige Nettobeträge gelten nicht); zum Buchen
  `_checked_mixed_groups` (jede Gruppe Brutto > 0, sonst `MIXED_GROUP_NOT_POSITIVE`; `MixedNotBookable` = `ValueError` mit festem
  Klartext). Satz in `Bookings.TaxRate` in EINER Form (`tax_groups.booking_rate`: 0,19 / 0,07 / 0,055, 0 % = NULL).
- **Grundsatz:** Ein Weg, der so einen Beleg bucht, bucht je Satz oder lehnt ab – nie mit dem Kopfsatz. Auswertungen unverändert
  (`tests/test_mixed_tax_reports.py`).
- **Versenden/Wiederbeleben:** je Gruppe Forderung „10000 an Wartekonto“ (`_book_mixed_receivable`: 4405 für 19 %, 4345 für 7 %),
  Wartekonto nur bei GENAU diesem Satz (`_group_waiting_entry`, nie `_tax_rate_key`). Fehlt es einer Gruppe: gar keine Forderung,
  Zahlung bucht je Satz „Erlöskonto an Bank“. Konten vor der Transaktion (`_mixed_receivable_accounts`); die Transaktion vergleicht den
  Stand der Gruppen (Schlüssel `('mixed', Gruppen)`, sonst `CHANGED_MESSAGE` 409; auch `ensure_receivable_booking`).
- **Zahlung** (`_assign_in_tx` → `_assign_mixed`, nur über das Kennzeichen; Kennzeichen und Kontext `mixed` müssen passen, sonst
  „geändert“): „Bank an Debitoren“ gesamt + je Satz „Wartekonto an Erlöskonto“ (`payment_rows(parts=…)`), ohne Forderung je Satz
  „Erlöskonto an Bank“.
  - Anteil: reine Funktion `tax_groups.split_payment`. Betrag: schließende Zahlung schließt jede Gruppe mit ihrem Rest; sonst je Satz
    auf den Anteil am Brutto, den die Summe der Zahlungen erreicht (**genau ein halber Cent nach unten**); Cent-Rest an die Gruppe mit
    größtem offenen Betrag (Gleichstand höherer Satz). Steuer: schließender Teil den Rest, sonst anteilig, höchstens den Rest (nie
    Betrag 0 mit Steuer).
  - Eingabe: `_mixed_booked` liest die markierten Sätze; Zuordnung ohne sie, fremder Satz, nicht aufgehende Summe:
    `MIXED_ALLOCATION_UNREADABLE` (die App rät nicht); Forderungen `_receivable_sides_by_rate` (genau die Sätze des Belegs, sonst
    `MIXED_RECEIVABLE_MISMATCH`).
  - **Erlöskonto je Satz:** `_group_revenue_coa` (`resolve_revenue_coa` über `booking_rate`; 0 % hat bewusst keines, nie 4184/4185).
    Der Nutzer wählt keines (`revenue_coa_choice(mixed=…)` leer, `MIXED_NO_REVENUE_COA`). Gelernt wird aus `Bookings.TaxRate` des
    Satzes (`_learned_revenue_coa`), nie aus dem Kopf.
  - Bankzeile bekommt nur Kunde und Beleg-Nr. (`_backfill_booking_skr`). Entfernen nimmt die ganze Zuordnung mit; Stufe 7 löst die
    Forderungszeilen gemeinsam (`zahlungen.md`).
- **Stornieren** (`cancel_invoice`): Vorprüfung liest die Gruppen (nicht buchbar: 409 `MIXED_GROUPS_MISMATCH`/`MIXED_GROUP_NOT_POSITIVE`
  vor dem Auflösen der Konten); die Transaktion vergleicht den Stand (`CHANGED_MESSAGE`). Stornorechnung: Kennzeichen, negierte
  Positionen und Summen, Gruppen = negierte (`round_minor`, halb von null weg). Gegenbuchung (nur bei stehenden Forderungen der App):
  je Gruppe „10000 an Wartekonto“ negativ mit Steuer und Satz. Ergeben die Positionen nicht die Summen: `MIXED_GROUPS_MISMATCH`, alles
  zurück, keine Nummer verbraucht.
- **Erstattung** (`assign_storno_refund`, `_refund_plan` → `_mixed_refund_parts`; trägt nur EINER der Belege das Kennzeichen:
  `MIXED_GROUPS_MISMATCH`): Verteilung über `split_payment`; Topf je Satz = was die Zahlungen der stornierten Rechnung getragen haben
  (`_mixed_booked`), nicht das Brutto; frühere Erstattungen zählen als gebucht; die Erstattung, die das Guthaben ausgleicht, nimmt von
  jedem Satz den ganzen Rest samt Steuer. Gebucht „Bank an Debitoren“ gesamt + je Satz „Wartekonto an Erlöskonto“ (negativ; ohne
  Gegenbuchung je Satz „Erlöskonto an Bank“); Wartekonten = die der Gegenbuchung, sonst `MIXED_RECEIVABLE_MISMATCH`. Erlöskonto je Satz
  = das der Zahlungen (`_refund_revenue_coas_by_rate`); mehr als eines/kein Erlöskonto: `MIXED_REFUND_REVENUE_AMBIGUOUS`; der Nutzer
  wählt keines (`revenue_coa_id` gilt nie, `refund_revenue_coas_differ` immer falsch). Aufteilung geht nicht auf: `MIXED_REFUND_SPLIT`;
  Zuordnung ohne Sätze der App: `MIXED_ALLOCATION_UNREADABLE`. Die Transaktion plant neu und vergleicht mit der Vorprüfung.
- **Neu ausstellen/Umzug:** `create_replacement_draft` kopiert das Kennzeichen, bucht nichts. Zahlungen ziehen nicht um, wenn die
  stornierte ODER die neue Rechnung ein Kennzeichen trägt (`_move_replaced_payments` bucht mit EINEM Satz/Erlöskonto; je Satz zu
  verallgemeinern ist offen und berührt eine nicht zu lockernde Regel). Hinweise `MOVE_MIXED_HINT`, `MOVE_REFUNDED_HINT`,
  `MOVE_NONE_HINT`; `_payment_move_context` löst für den Platzhalter nie ein Erlöskonto auf.
- Zuordnung neu aufbauen: nur offener Betrag (Grund `mixed`). Prüfliste: `rate_split_mismatch`, `rate_refund_mismatch`,
  `unflagged_rate_mix` (alle `zahlungen.md`).
- **Ein Weg lehnt ab** (`MIXED_LINK_NOT_BOOKABLE`, vor dem ersten Schreiben, am frisch gelesenen Beleg):
  `link_invoice_to_transaction` (WISO-Import). `update_invoice_status` (ungeprüfter Baustein) bleibt unverändert.
- **PDF:** nur mit dem Kennzeichen, nie nach den Positionssätzen; ohne Kennzeichen druckt es wie sonst (verschiedene Positionssätze:
  Server-Log `einvoice_model.log_unflagged_rates`). Spalte „USt“ (`pdf_document.COL_RATE_R`, `_rate_layout` einmal je Beleg),
  Summenblock je Satzgruppe „zzgl. 7 % MwSt. auf 50,00 EUR:“ (Seitenumbruch; ab 46 Gruppen Folgeseite, der Gesamtbetrag nie allein);
  der Kopfsatz wird nie gedruckt. Gruppen aus `tax_groups.item_groups` (dieselbe Stelle wie die Buchungen) über
  `einvoice_model.per_item_rates`; weicht eine vom Cent der E-Rechnung/den Summen ab, druckt das PDF trotzdem, das Server-Log nennt es.
  Beispiele `zugferd-mischsatz.pdf`, `xrechnung-mischsatz.xml`.
- **Maske:** Häkchen `#mixed_tax` (nur mit Steuerausweis, in gesperrter Rechnung gesperrt, vorbelegt aus `has_mixed_rates`) schaltet
  Spalte „USt“ (`mixedTaxOn`, `buch.css`) und Satzzeilen ein (nur Anzeige, der Server rechnet). Auswahl vom Server über
  `script_json` aus den Stammdaten (`entry_choices(db)`, sonst `MIXED_TAX_RATES`), im Skript steht kein Satz. Ohne Häkchen der
  Standard-Payload, mit Häkchen `taxMode: 'mixed'` und Satz je Position. Ein gespeicherter Satz außerhalb der Liste erscheint als
  eigene gewählte Option (nichts still umgedeutet, der Server lehnt ab). EIN Skriptblock für beide Masken (`MIXED_TAX_JS` in
  `server/pages_invoice.py`, nur `var`/`function`, Präfix `mix`).
- Tests: `tests/test_mixed_tax_save.py`, `tests/test_mixed_tax_locks.py`, `tests/test_mixed_tax_bookings.py`, `tests/test_mixed_tax_reports.py`, `tests/test_mixed_tax_storno.py`, `tests/test_mixed_tax_documents.py`, `tests/test_mixed_tax_mask.py`,
  `tests/test_tax_groups_split.py`.

## Steuersätze aus den Stammdaten

- Weitere Sätze (5,5 % …) und steuerfreie Leistungen aus Stammdaten → Steuersätze (`TaxRates`, `/masterdata/taxrates`,
  `db/tax_rates.py`: `TaxRatesMixin`, `TaxEntry`, `TaxRateRefused`), für den ganzen Beleg oder je Position, mit 19/7 % mischbar.
- **Eintrag:** Bezeichnung, Satz in Hundertstel Prozent (`RateBp`: 1900 = 19 %; aus Text nur `tax_groups.parse_rate_bp`, Anzeige
  `bp_pct`), Art `standard`/`exempt` (steuerfrei nur mit Satz 0 und Pflicht-Grund), Erlöskonto (Pflicht außer 19/7 % ohne eigene
  Angabe: `has_default_revenue`), optional Umsatzsteuer-/Vorsteuerkonto (nur 3800–3849 bzw. 1400–1439, nie bei Satz 0 und nie bei
  Sätzen mit festen Steuerkonten 19/16/7/5 %, `FIXED_TAX_BPS`), Reihenfolge, „wählbar“. 19/7 % sind einmal beim Anlegen der Tabelle
  vorbelegt.
- **Die Regeln eines Eintrags stehen an EINER Stelle** (`tax_groups.check_tax_entry`, feste Sätze `TAX_ENTRY_*`; Satz zwischen 0 und
  1 % gilt nicht; Bezeichnung/Grund ohne Steuer-/Formatzeichen), gelten beim Schreiben wie Lesen (`TaxEntry.valid`; wählbar = gültig UND
  aktiv; ungültige Zeilen aus der SQL-Konsole stehen in keiner Auswahl). Der letzte wählbare Eintrag bleibt
  (`TAX_ENTRY_LAST_ACTIVE`); sonst frei änderbar/löschbar – **kein Beleg verweist auf einen Eintrag**. Kein Befreiungscode (BT-121).
- **Momentaufnahme:** Die Position trägt nie die ID, sondern den Stand beim Speichern: `InvoiceItems.TaxRateBp` (Index 11),
  `TaxExemptReason` (12), `RevenueCoaId` (13, ohne Fremdschlüssel; `coa_is_referenced` schützt das Konto), `TaxCategory` (9, `'E'` =
  steuerfrei) – `db.invoice_payments.ITEM_SNAPSHOT_KEYS`, nur vom Server gebaut (`db.tax_rates.entry_snapshot`).
  - **Merkmal ist `TaxRateBp IS NOT NULL`**, nie `'E'` allein (das § 19-Beispiel schreibt `'E'` ohne Momentaufnahme). Gültig: 0 oder
    100–10000 und gleich `rate_pct(TaxRate)` × 100 (`invoice_payments.valid_rate_bp`, `einvoice_model.item_bp`; ein Test hält beide gleich).
  - Stornorechnung, Ersatzrechnung, Angebot → Rechnung kopieren alle vier Spalten; **jeder neue Kopierweg einer Position muss es auch
    tun** (`tests/test_tax_rates_schema_2008.py`). Stammdaten-Änderungen wirken nur auf neue/neu gespeicherte Entwürfe.
- **Speichern** (`handlers._tax_fields(db, data, items)`, EINE Stelle für Rechnung und Angebot): `taxRateId` im Kopf (nur ohne
  `taxMode`; Kennzeichen NULL – ganz steuerfreier Beleg mit Kopf 0) oder je Position (nur mit `taxMode: 'mixed'`). Eintrag muss
  wählbar sein, Satz des Formulars = Satz des Eintrags, `0` immer Ablehnung, ohne Steuerausweis keine ID (`TAX_CHOICE_*`).
  - **Je Satz höchstens EINE Momentaufnahme – auch „mit“ gegen „ohne“** (`TAX_SAME_RATE_TWICE`): nie zwei 0-%-Arten, zwei
    Befreiungsgründe, gemischte Rundung. Dokumente und Buchen verlassen sich darauf – nicht lockern.
  - Payload ohne `taxRateId`: gespeichert wie ohne Stammdaten (Entwurf verliert Momentaufnahmen). Sperre versendeter Rechnungen
    vergleicht die vier Spalten mit (`invoice_lock.stored_items`, `_items_differ`).
  - Ein Eintrag, den der Standard-Payload trägt (`legacy_expressible`: 19/7 % ohne Erlöskonto), schreibt keine Momentaufnahme
    (`handlers._snapshot_to_write`) – „öffnen und unverändert speichern“ ändert keine Spalte; `TAX_SAME_RATE_TWICE` gilt nach dem
    Gespeicherten.
  - Das Erlöskonto jeder zu schreibenden Momentaufnahme wird zuerst in der Schreibtransaktion geprüft
    (`_refuse_gone_revenue_accounts`, `Database.missing_revenue_coas`): fehlt es `TAX_CHOICE_CHANGED`, nichts gespeichert.
- **Maske:** `#tax_choice` (`taxChoice`) wählt den Eintrag im Kopf, mit „Steuersatz je Position“ je Zeile; `#tax_rate` bleibt verstecktes
  Feld (bei gesperrter Rechnung `disabled`), `#tax_choice` schreibt bei `change` hinein und ruft `calculateTotals()`. Liste =
  `entry_choices(db)` als `MIX_RATES` über `script_json` (im Skript kein Satz, keine ID, keine Bezeichnung).
  - Ein `legacy_expressible`-Eintrag hat als Optionswert den Satz, keine ID: **der Payload für 19/7 % bleibt Zeichen für Zeichen gleich**
    (`tests/test_mixed_tax_mask.py`); jeder andere `e<ID>`, `taxRateId` als letzter Schlüssel.
  - **Ein geladener Beleg wählt den Eintrag nach seiner Momentaufnahme** (`entry_snapshot` gleich Art, Satz, Grund, Erlöskonto; auch bei
    „Als neu kopieren“, Angebot → Rechnung, Ersatzrechnung). Gibt es ihn nicht mehr: Option `e0` (`taxRateId: 0`, der Server lehnt ab,
    bis neu gewählt ist). Ohne Momentaufnahme: Vorbelegung nach Satz, sonst Satz als eigene Option ohne ID.
  - Zwei Skriptkonstanten in EINEM Block (`MIXED_ENTRY_JS` vor `MIXED_TAX_JS`); Befreiungsgrund `#tax_note` über `textContent`.
    `buch.css`: `.invoice-items .item-tax` ≤ 130px, `.taxChoice` ≤ 260px.
  - **Platzhalter (`__…__`) nur in konstanten Skriptblöcken ersetzen, nie per `s.replace` über eine Seite mit Datenbankwerten**
    (`tests/test_mask_placeholder_values.py`).
- **Rundung – zwei Regeln, eine Stelle** (`tax_groups.group_tax(net, pct, exact)`): Eine Gruppe, deren Positionen ALLE eine
  Momentaufnahme tragen, rechnet einstufig (Netto × Satz, EINMAL auf den Cent, halb weg von null); jede andere zweistufig wie ohne
  Stammdaten. Gleich bei ganzzahligem Satz und Netto in ganzen Cent (`tests/test_tax_entries_pure.py`); 5,5 % weicht ab. `rate_groups`,
  `item_groups`, `mixed_groups` nehmen ein drittes Element (`exact` bzw. `bp`); `recompute_invoice_totals(items, default, snapshots)`.
  **Jede Stelle, die Gruppen aus Positionen liest, liest `TaxRateBp` mit** – sonst laufen Summen, Dokument, Buchung auseinander. Sätze in
  Texten nur über `pct_text`/`group_label`.
- **Buchen:** Das Erlöskonto der Momentaufnahme gilt bei Zahlung und Erstattung immer – nie 4184/4185, nie gelernt, keine Nutzerwahl
  (`revenue_coa_choice(…, entry=…)`); ein Stammdaten-Erlöskonto lehrt keines (`_learned_revenue_coa`).
  - Kein Erlöskonto (mehr): `TAX_PAYMENT_NO_REVENUE`. **Das Versenden prüft vorab** (`_entry_revenue_blocker`, 409
    `TAX_SEND_NO_REVENUE`, vor dem Anlegen von Konten und dem ersten Schreiben).
  - **Wartekonten bleiben fest:** 19 % → 4405, 7 % → 4345, 0 % (steuerfrei) → 4340, genau (`_group_waiting_entry`). Jeder andere Satz hat
    keines: keine Forderung, Zahlung „Erlöskonto an Bank“ je Satz, `NO_WAITING_HINT`. Ein Wartekonto je Eintrag gibt es bewusst nicht
    (EÜR, SuSa, DATEV-Kette und Storno erkennen Wartekonten an festen Nummern).
  - **Widersprüche der Momentaufnahme** (verschiedene Angaben in einer Gruppe, teils mit/teils ohne, halbe Angabe, `'E'` mit Satz ≠ 0,
    unlesbarer/unpassender Satz, Kopf passt nicht) lehnen Versenden, Zahlung, Stornieren vor dem ersten Schreiben ab
    (`TAX_ENTRY_CONTRADICTION`; `MIXED_GROUPS_MISMATCH` bleibt für „Positionen ≠ Summen“). Die **Erstattung prüft sie bewusst nicht**.
  - Umzug unterbleibt bei Stammdaten-Erlöskonto (`MOVE_ENTRY_HINT`); „Zuordnung neu aufbauen“ rechnet dann nur den offenen Betrag (Grund
    `tax_entry`) – Bedingungen beider Wege nicht lockern. Prüfliste `tax_entry_mismatch` (feste Sätze `TAX_ENTRY_FINDINGS`; `'E'` ohne
    Angaben ist kein Befund, die halbe Angabe schon). Meldungen „steuerfreie Leistungen“ (`rate_sentence`), Listen/PDF „steuerfrei“
    (`group_label`).
- **Dokumente:** Satz der Position aus der Momentaufnahme; steuerfrei = Kategorie E, Befreiungsgrund BT-120 aus EINER Funktion für PDF und
  XML (`einvoice_model.exempt_reason`: erster nicht leere Grund der steuerfreien Positionen, bereinigt, ≤ `MAX_REASON_LENGTH` plus „…“),
  nie der § 19-Text. Fehlt er: `einvoice_gaps` meldet es, UBL entsteht nicht. PDF: Summenzeile „steuerfrei auf … EUR:“, Grund unter dem
  Summenblock (≤ 12 Zeilen). Ein Beleg OHNE Momentaufnahme ergibt Byte für Byte dieselben Dokumente wie ohne Stammdaten. Beispiele
  `zugferd-satz-5-5`, `zugferd-steuerfrei-gemischt`, `zugferd-steuerfrei` (Einstufig-Regel: `tests/test_tax_entries_demo_samples.py`).
- **DATEV:** Satz außerhalb 19/16/7/5 % bekommt keinen BU-Schlüssel (0; bei Umsatzsteuer der Satz in Spalte 40); der Export zählt solche
  Zeilen (`export_to_datev(rates_without_key=…)`, `DatevStack.hint`), fester Hinweis `DATEV_RATE_NOTE`; der Stapel bleibt Byte für Byte
  gleich. Weitere Auswertungen: `auswertungen.md`.
- **Bekannte Grenzen:** kein Wartekonto für weitere Sätze; ein Eintrag mit genau 100 % ist speicherbar, der Beleg aber nicht versendbar;
  Reverse Charge, innergemeinschaftliche Lieferung, Ausfuhr fehlen (`docs/aufgaben.md`). Keine Steuerberatung in Texten.
- Tests: `tests/test_tax_entries_pure.py`, `tests/test_tax_rates_schema_2008.py`, `tests/test_tax_rates_db.py`, `tests/test_tax_rates_page.py`,
  `tests/test_tax_entries_save.py`, `tests/test_tax_entries_mask.py`, `tests/test_tax_entries_storno.py`, `tests/test_tax_entries_bookings.py`, `tests/test_tax_entries_reports.py`, `tests/test_tax_entries_documents.py`, `tests/test_tax_entries_demo_samples.py`,
  `tests/test_tax_accounts_reports.py`, `tests/test_standard_accounts_purchase.py`.

## Abweichung vom Artikel (Maske von Rechnung und Angebot)

- **Reine Anzeige:** Eine Position, die vom gespeicherten Artikel abweicht, fällt in der Maske auf; Speichern, Payload, Sperre und Summen bleiben, wie sie waren (`tests/test_mixed_tax_mask.py`,
  `tests/test_tax_entries_mask.py`). EIN Skriptblock für beide Masken (`ARTICLE_MARK_JS` in `server/pages_invoice.py`, im Block von `MIXED_TAX_JS`, nur `var`/`function`, Präfix `art`, Texte
  `ARTICLE_TEXTS` über `script_json`). Das Zeilen-Markup des Servers bleibt unverändert: Klassen, Tooltips und das „+“ setzt allein das Skript. `artMark()` läuft nach dem Laden, nach dem Hinzufügen und Entfernen
  einer Zeile und bei jedem `input`/`change` der Tabelle (Zeilen, „Steuersatz je Position“, „Umsatzsteuer ausweisen“, Feld „Mehrwertsteuer“ im Kopf).
- **Welcher Artikel (`artFind`, rein):** `data-article-id` der Zeile (aus der Artikelsuche), sonst beginnt die Beschreibung – normalisiert: außen ohne Leerraum, Leerraum zusammengefasst, `toLowerCase()` – mit dem
  Namen eines aktiven Artikels, und danach folgt das Ende oder ein Zeichen, das weder Buchstabe noch Ziffer ist („Beratung“ trifft nicht „Beratungspaket“). Mehrere: der längste Name, gleich lange keiner. Namen
  unter 3 Zeichen nehmen nur über die ID teil.
- **Was markiert wird:** Menge und Beschreibung nie. Ein Feld, das abweicht, bekommt `itemDeviates` und den Tooltip „Artikel: …“ (er verschwindet mit der Abweichung; das Auswahlfeld des Satzes behält den Tooltip von
  `mixTotals`): Einheit (außen ohne Leerraum, Kleinbuchstaben), Netto-Einzelpreis (in ganzen Cent), Steuersatz – mit „Steuersatz je Position“ die Auswahl der Zeile gegen `mixRate` des Artikels, sonst der Satz des Belegs
  (Zelle der Positionsnummer). Ohne Steuerausweis (§ 19) und bei einem Artikelsatz, der sich nicht lesen lässt, keine Steuer-Markierung. Eine Zeile mit Beschreibung ohne Artikel trägt `itemNoArticle` (Rand links);
  eine Zeile ohne Beschreibung bleibt unmarkiert. **In einer gesperrten Rechnung (`#invoice_locked`) läuft nichts, es gibt kein „+“.** `buch.css`: Das Blatt ist in beiden Farbschemata weiß (`.invoice-container`),
  die Regeln brauchen `!important` gegen `.invoice-container *`; der Platz des „+“ bleibt reserviert (`visibility`), sonst spränge die Spalte beim Tippen.
- **„+“ (`.itemAddArticle`, sichtbar an einer Zeile mit `itemCanSave`):** nach Rückfrage `POST /articles/quick-add` (`_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`: GET 405, Herkunft und CSRF-Token wie jede JSON-Route;
  JSON `{name, unit, price, taxRate}`, `handlers.handle_article_quick_add`): `name` ist die Beschreibung der Zeile (Leerraum zusammengefasst, höchstens 200 Zeichen), `unit` ihre Einheit, `taxRate` der Satz der
  Zeile bzw. des Belegs (ohne Steuerausweis 0); der Server verlangt Name 1–200 und Einheit 1–20 druckbare Zeichen, Preis 0 bis unter 10⁹ über `money` auf den Cent gerundet, Satz 0–100; jede andere Form 400 mit
  festem Satz, nichts angelegt. **Es wird nur angelegt, nie ein vorhandener Artikel geändert:** Gibt es schon einen mit derselben Bezeichnung (aktiv oder nicht, ohne Rücksicht auf Schreibweise und Leerraum:
  `db.articles.article_key`, dieselbe Regel wie `artNorm`), 409 `ARTICLE_EXISTS`. Prüfung und Anlegen laufen in EINER Transaktion (`insert_article_unique`, `BEGIN IMMEDIATE`). Antwort: der Artikel in der Form
  eines Eintrags von `articlesData` (`pages_invoice.article_json`: EINE Funktion für beide Masken und die Antwort). Im Demo-Modus erlaubt (keine Datei).
- Tests: `tests/test_article_marks.py`, `tests/test_article_quick_add.py`.

## Pflichtangaben und Hinweise im Dokument

- **§ 19-Hinweis:** Beleg ohne Steuerausweis (`Invoices.TaxRate` = Platzhalter −1) trägt ihn automatisch (Rechnung, Stornorechnung,
  Angebot). EIN Wortlaut an EINER Stelle (`export/einvoice_model.EXEMPT_REASON_KLEINUNTERNEHMER`) für PDF (`draw_item_table(…,
  tax_note=…)`), CII (BT-120), UBL.
  - `small_business_note` entscheidet: nur beim Platzhalter (nicht 0 %); nicht gedruckt, wenn ein gedruckter Freitext (Rechnung:
    Zahlungshinweis; Angebot: Einleitung, Schluss) das Gesetz zitiert („§ 19“/„§ 19a“, höchstens „Abs./Satz n“, direkt gefolgt von
    „UStG“/„Umsatzsteuergesetz“), geprüft am gedruckten Text (Richtext erst durch `sanitize_richtext`). Positionstexte, „Kleinunternehmer“
    allein, „§ 19 BGB“ unterdrücken nichts. **Ein fehlender Hinweis wiegt schwerer als ein doppelter: Erkennung nicht lockern.**
  - Häkchen eines neuen Belegs (ohne Quelle) folgt der jüngsten gewöhnlichen Rechnung (`Database.last_document_shows_tax`: keine
    Storno-/Ersatzrechnung, kein Angebot; ohne eine an); startet es abgehakt, sagt die Maske das sichtbar.
- **Steuernummer vs. USt-IdNr.** der eigenen Firma unterscheidet EINE Stelle (`einvoice_model.tax_id_kind`): vorangestellte Bezeichnung
  fällt weg; USt-IdNr. nur mit bekanntem Länderpräfix und dessen Format (DE + 9 Ziffern), alles andere Steuernummer. Gilt für
  Momentaufnahme und Kontakt-Rückgriff, CII (`VA`/`FC`), UBL (`VAT`/`TAX`), PDF-Fuß (liest die Momentaufnahme, den Kontakt nur wenn
  leer). Nur Steuernummer: zusätzlich BT-29 (`Party.identifier`, sonst lehnt der Validator ab, BR-CO-26). Eine Käuferangabe, die keine
  USt-IdNr. ist, geht nicht in die E-Rechnung.
- **Skonto-Bedingungen** (`Invoices.SkontoDays`/`SkontoPercent`, `parse_skonto`): Tage 1–365, Prozent 0–100 mit ≤ 2 Nachkommastellen,
  geprüft am Text bzw. an der Zahl, BEVOR umgerechnet wird (`1e9999999`); nur beim Speichern einer nicht eingefrorenen Rechnung, nur
  bei Brutto > 0 und gültigem Rechnungsdatum; Frist nicht nach der Fälligkeit. PDF: Absatz nach dem Zahlungshinweis (Abzug in EINEM
  Schritt halb auf den Cent, `skonto_amount`). E-Rechnung: erste Zeile der Zahlungsbedingungen (`#SKONTO#TAGE=7#PROZENT=2.00#` +
  Zeilenumbruch). Die Stornorechnung trägt nie Skonto. Gebucht: `zahlungen.md`.
- **Lückenprüfung** (`einvoice_gaps`): Fehlen Angaben, die die XRechnung verlangt (Leitweg-ID/Käuferreferenz, IBAN – nur bei der Überweisung, PLZ/Ort,
  Kontaktname, Telefon und E-Mail der eigenen Firma, E-Mail des Kunden, Zahlungshinweis ohne „#“), wird die UBL-Datei **nicht
  geschrieben** (ein abgelegtes XML eines Entwurfs wird entfernt – nur `.xml`, nie das PDF); das PDF mit CII entsteht immer. Ergebnis
  trägt den Klartext („XRechnung (XML) nicht erstellt – es fehlt: …“); die Rechnungsseite zeigt die Lücken, vor dem Versenden fragt sie
  nach. UBL entsteht nur bei Leitweg-ID/Käuferreferenz (`wants_xrechnung`; Bestellnummer allein nicht). Kontoname (BT-85) wird nicht
  geschrieben. **Zahlungsart:** Eine Karte (48) ist eine Lücke der XRechnung (BR-DE-24 verlangt die Ziffern der Kartennummer, die die App nicht führt): kein UBL, die CII im PDF
  entsteht; eine nicht lesbare Zahlungsart ist eine Lücke auch der CII.

## Sperre versendeter Rechnungen (`db/invoice_lock.py`)

- **Eine Stelle entscheidet** (`check_changes`, Statusgruppen); jeder Schreibweg geht darüber: Speichern (`Database.update_invoice_fields`,
  bei versendeter nur aus `correct_invoice`) und Statuswechsel (`Database.set_invoice_status`) – nie `update_invoice_status`
  (ungeprüft, für Seed/Tests; Angebote `set_quote_status`).
- Bearbeitbar alles bei `draft`/`finalized`. Bei `sent`/`overdue`/`partial_payment`/`partial`/`paid` gesperrt: Nummer, Rechnungsdatum,
  Kunde, eigene Firma, Positionen, Steuer, Summen, Fälligkeit, Skonto, Bank, Zahlungsart, Währung, Käufer-USt-IdNr. (`/invoice/save`: **409** mit
  Klartext, nichts geschrieben; die Überweisung gilt in jeder Schreibweise als dieselbe – NULL, leer, `'58'`). Nur als **Berichtigung** änderbar: Zahlungshinweis (`PaymentTerms`), Anschrift, Bestellnummer,
  Leitweg-ID, Leistungsdatum, Leistungszeitraum. `cancelled` und Stornorechnung (`row_group`: `InvoiceKind='storno'` gilt als abgeschlossen): gar nichts.
  Unbekannter Status gilt als gesperrt.
- Geprüft gegen den gespeicherten Stand **in der Transaktion** (`BEGIN IMMEDIATE`, `UPDATE` trägt die Statusgruppe im `WHERE`).
  Unverändert Zurückgeschicktes ist kein Verstoß (Minor Units, Positionen Feld für Feld, `unit_display`, 0,19 = 19).
- **Die Maske überschreibt bei versendeter Rechnung nichts still:** zeigt gespeicherte Werte, holt nichts aus den Stammdaten, schickt
  nur geänderte Felder (kein `items`); der Server schreibt nur Abweichendes (`changed_open_fields`) – unverändert ändert keine Spalte
  (auch nicht `NULL` zu leer) und legt weder Fassung noch Protokoll an.
- Anschrift = Momentaufnahme; neu aus den Stammdaten **desselben** Kunden nur auf ausdrücklichen Wunsch (`refreshAddress`, mit alt →
  neu). Absender, Bank, Käufer-USt-IdNr. sind abgeleitet und werden bei Sperre nie neu geholt.
- **Status und `AmountDue` kommen nie vom Client:** neue Rechnung `draft` (oder `finalized`), `AmountDue` im Entwurf das Brutto, danach
  die Zahlungslogik.

## Berichtigen (`Database.correct_invoice`, `db/invoice_versions.py`)

- Keine eigene Schreibroute: `/invoice/save` mit Teil-Payload (Knopf „Berichtigen“, nach Rückfrage). Gesperrte Felder zuerst
  abgelehnt; ohne Abweichung passiert nichts (auch ohne `documentVersion`). **Das Leistungsdatum bleibt ein Datum:** unter den
  geänderten Angaben ISO, sonst `CorrectionInvalid` (400, vor dem Verschieben der Dokumente); unverändert (auch leer) ist kein Verstoß.
  Das Ende des Leistungszeitraums gilt mit dem Beginn, neu oder gespeichert (`normalize_service_end`): ein Ende, das kein Datum ist oder vor dem Beginn liegt, ein Beginn hinter dem
  gespeicherten Ende und ein Ende ohne Beginn sind ebenso `CorrectionInvalid`; ein Ende am Beginn ist kein Zeitraum (NULL, bei gleichem gespeicherten Wert ändert sich nichts).
  Protokoll „Leistungszeitraum (bis)“, Hinweis der Fassung „Leistungszeitraum“.
- In EINER Transaktion (`BEGIN IMMEDIATE`, Rechnung neu gelesen): geladene Fassung (`documentVersion`; `Invoices.DocumentVersion`, NULL =
  1) muss die aktuelle sein, sonst 409 (fehlt/veraltet: nichts geschrieben); PDF/XML nach `Fassungen/<ID>/` (`archive_sent_documents`)
  samt Fassung, Pfad (relativ zu `invoices/`), SHA-256 in `InvoiceDocumentVersions`, Pfade in der Rechnung geleert; je geändertes Feld
  eine Zeile `InvoiceCorrections` (`Action='correct'`; `refreshAddress`: je Anschriftsspalte); `DocumentVersion` +1. Scheitert
  Verschieben, `_archive_documents` oder Commit: Dateien zurück, 409, nichts geändert.
- Danach erzeugt `handlers._correct_invoice` das Dokument der neuen Fassung – nur, wenn eines abgelegt war (Import: kein Dokument).
  Scheitert das: Berichtigung bleibt, Antwort trägt Warnung, „PDF + E-Rechnung“ erzeugt nach.
- **Die berichtigte Fassung trägt einen Hinweis** (PDF: Zeile vor der Positionstabelle und „Fassung: n“; CII BT-22; UBL `cbc:Note`;
  TypeCode 380 und Nummer bleiben), abgeleitet aus dem Datenbankstand (`correction_notice`: `DocumentVersion` > 1 und Berichtigungen zur
  aktuellen Fassung, keine Rückkehr auf Entwurf danach), nicht aus einem Parameter. „Ersetzt die Fassung vom …“ = jüngster
  Protokolleintrag vor der ersten Berichtigung der aktuellen Fassung, der Berichtigung oder Versand ist, sonst `SentAt`, sonst entfällt
  der Teil. Zeiten UTC gespeichert, Ortszeit angezeigt.
- Die stornierte Rechnung bleibt ganz gesperrt. Eine Ersatzrechnung trägt daneben „Ersetzt die stornierte Rechnung …“ (beide Hinweise,
  der der Ersetzung zuerst).

## Statusübergänge (`db.invoice_lock.TRANSITIONS`)

- Auswahl bietet nur erlaubte Ziele; `set_invoice_status` lehnt den Rest ab (409; „bezahlt“ ohne Zahlung 400). Entwurf ↔ abgeschlossen frei.
- **Entwurf/abgeschlossen → versendet** ist der einzige Übergang, der die Forderung bucht und das Dokument erzeugt. Versendet ↔
  überfällig bucht und erzeugt nichts.
- Teilzahlung/bezahlt und Rückweg setzt die Zahlungslogik (`next_payment_status`); „bezahlt“ von Hand nur mit ≥ 1 Zahlung (Skonto);
  Rückweg bezahlt/Teilzahlung → versendet/überfällig von Hand nur ohne Zahlung. Versendet/überfällig → Entwurf/abgeschlossen
  („zurückholen“) nur ohne Zahlung: Forderung entfernt, Dokumente nach „Fassungen“. **Stand die Forderung schon in einem DATEV-Export**
  (`BookingExports`), fragt `set_invoice_status` zuerst nach: `409` mit `REOPEN_EXPORTED_QUESTION` und `confirm == 'exported'`, nichts
  geändert; erst `confirm_exported: true` (JSON-Wahrheitswert, sonst `InvalidRequest`) lässt es geschehen, die Seite fragt über
  `appConfirm` und schickt neu. Keine Sperre; ohne Export keine Rückfrage; die Ablehnungen (`REOPEN_*`) gehen ihr vor
  (`tests/test_reopen_after_export.py`).
- **Nach „storniert“ nur für Rechnungen ohne positiven Betrag** (Brutto ≤ 0), Art `cancel`: Forderung geht, solange keine Zahlung hängt
  und nur die der App. `check_transition` entscheidet am Betrag, den `set_invoice_status` in der Transaktion liest (Bedingung
  `NO_POSITIVE`; `check_transition`/`allowed_targets` nehmen `gross`, ohne Angabe positiv). Brutto > 0: `set_invoice_status(…,
  'cancelled')` lehnt mit 409 ab (storniert wird über „Stornieren“); Knopf nur bei Brutto > 0, „Storniert“ in der Auswahl nur bei ≤ 0.
- Storniert → versendet/überfällig („wiederbeleben“) nur ohne Stornorechnung: bucht die Forderung neu, lässt ein Dokument stehen; mit
  Stornorechnung 409. Die Stornorechnung ändert ihren Status nie (409).
- **Status, Forderungsbuchung und (beim Zurückholen) das Ablegen der Dokumente in EINER Transaktion** (`set_invoice_status`); nur das
  Erzeugen des Dokuments beim Versenden folgt danach (`handle_update_invoice_status`). **Jeder gelungene Wechsel steht im Protokoll**
  (`InvoiceCorrections`, `Action='status'`, selbe Transaktion; `_recalc_invoice` protokolliert nicht); `send` setzt `SentAt` beim ersten
  Versand. Konten, die das Buchen anlegen könnte, vorher in eigenen Verbindungen (`_receivable_accounts`), nie innerhalb.
- **Die App entfernt nur die Forderung, die sie selbst gebucht hat** (`_receivable_removal`: ohne `SourceGroup`, Wartekonto als
  Gegenkonto, keiner Bankbewegung zugeordnet) – beim Zurückholen, Storno von Hand (Brutto ≤ 0), Entfernen der letzten Zahlung einer von
  Hand stornierten Rechnung (`_drop_receivable`): nie ein Satz mit Quellklammer. Hängt eine importierte, von Hand gebuchte oder an
  eine Bankbewegung gehängte Forderung oder ein Zahlungssatz an der Rechnung, lehnt das Zurückholen ab (409); Stornieren, Storno von
  Hand, Zahlungsentfernen lassen sie stehen.

### Umzug der Zahlungen beim Versenden einer Ersatzrechnung

- Beim Versenden (`ReplacesInvoiceId`, Art `send`) **ziehen die Zahlungen der stornierten Rechnung um** – in derselben Transaktion, nach
  der Forderungsbuchung (`_payment_move_context` löst das Erlöskonto vorher in eigenen Verbindungen auf, `_move_replaced_payments`
  schreibt; `update_invoice_status` zieht nie um). Voraussetzung: gleicher Kunde, gleiche Währung, hinterlegtes Erlöskonto für den Satz
  der neuen Rechnung – sonst bleibt alles, der Hinweis sagt es. Bei Steuersatz je Position oder Stammdaten-Erlöskonto zieht nichts um.
- **Nach einer Erstattung zieht höchstens das verbleibende Guthaben um:** `movable_left` = alle Zuordnungen der stornierten Rechnung
  (auch negative des WISO-Imports: ziehen nie um, mindern das Guthaben) minus Erstattungen an der Stornorechnung, vor dem Umzug; je
  Zuordnung `min(Betrag, Rest der neuen Rechnung, movable_left)`, bei `<= 0` Ende, danach sinkt `movable_left`; der Rest bleibt an der
  stornierten Rechnung (`allow_cancelled`). Nie mehr erstattet als gezahlt; ohne Erstattung bremst `movable_left` nie.
- Umgezogen wird je Zuordnung (älteste zuerst, solange die neue Rechnung offen ist) und nur, was die App **genau so geschrieben hat**
  und sich eindeutig neu schreiben lässt (`_movable_payment`: Bankbewegung mit Zahlungseingang und SKR-Konto, nicht überbucht, ohne
  Sätze mit `SourceGroup`, genau eine Zuordnung zum Paar).
  - **Die Sätze müssen genau die von `_assign_in_tx` geschriebenen sein** (`_revenue_coa_if_as_written`, Vergleich mit `payment_rows`):
    Bestand („Bank an Debitoren“ + „Wartekonto an Erlöskonto“ oder Einzelsatz „Erlöskonto an Bank“, keiner mehr/weniger), Bankkonto,
    Konten in ihrer Rolle, Betrag, Datum, Steuerdatum, Empfänger, Währung, Kunde, Text, Steuersatz, Steuerbetrag (anteilig; der Rest nur
    bei voll bezahlter Rechnung: `_prorated_tax`, `_remaining_tax`), Debitoren-/Wartekonto = die der Forderung (`_receivable_sides`),
    keine Kategorie.
  - Dazu: kein Beleg/Verweis an den Sätzen (`_booking_attachments_exist`, `BOOKING_REFERENCES`, `_delete_booking_rows`); unter der Bewegung
    kein markierter Satz mit einer Beleg-Nr., die zu keiner ihrer Zuordnungen gehört, und kein Satz ohne Marke, der die Beleg-Nr. der
    Rechnung trägt oder Debitoren berührt; bei gleichem Steuersatz muss das Erlöskonto der Sätze das heute hinterlegte sein.
  - Sonst bleibt die Zuordnung liegen (der Umzug löschte die Sätze). Beurteilt wird jede Zuordnung gegen den Stand VOR dem ersten Umzug.
    Importe, überbuchte Bewegungen, geänderte Sätze bleiben; eine Sammelzahlung zieht nur mit dem Anteil der stornierten Rechnung um.
- Je Zuordnung: samt Sätzen entfernen (`_unassign`), neue Rechnung bekommt, was sie braucht (`_assign_in_tx`: Datum der Bankbewegung,
  Satz und Erlöskonto der neuen Rechnung; Steuer anteilig, schließender Teil den Rest), ein Rest wird an der stornierten Rechnung wieder
  zugeordnet (`allow_cancelled`, bisheriges Erlöskonto) = ihr Guthaben.
- **Je Zuordnung ein Savepoint:** Lässt sie sich nicht vollständig mit den erwarteten Beträgen neu buchen, geht er zurück (Zuordnung,
  Sätze, Replay-Log-Einträge unverändert), sie zählt als übersprungen, der Versand gelingt; nur ein unerwarteter Datenbankfehler lehnt
  den Versand ab (409, `PaymentMoveFailed` → `_Refusal`, nichts geändert).
- **Was geschah, nennt `hint`** (`StatusChange.payment_hint`) mit Grund (`MOVE_SKIP_REASONS`: Import, Bankkonto ohne SKR-Nummer,
  überbuchte Bewegung, Sätze ohne Marke, geänderte/ergänzte Sätze oder Beleg, anderes Erlöskonto als hinterlegt, Sätze schon in einem
  DATEV-Export (Merkmal „exportiert“, `exported`), allein anderes Steuerdatum (`tax_date`: ein Export von vorher stempelte nur das Datum),
  mehrfach zugeordnete Bewegung, keine Bankbewegung mit Zahlungseingang, nicht neu buchbar; je Grund einmal, feste Reihenfolge, mit
  Gesamtbetrag). Ein Satz mit dem Merkmal „exportiert“ zählt als „hängt etwas dran“ (`_booking_attachments_exist`): Die Zuordnung zieht nicht
  um, und `_movable_payment` nennt `exported`, sobald ein Satz der Zuordnung (`_payment_children`) das Merkmal trägt (vor `tax_date`).
  Spätes Entfernen der Zahlung von Hand: nichts wandert zurück.

## Stornobeleg (`Database.cancel_invoice`, `db/invoice_storno.py`)

- **Route** `POST /invoice/cancel`, JSON `{"invoiceId", "date"}` (`invoiceId` streng Ganzzahl), in `_JSON_PATHS` und
  `_POST_ONLY_JSON_PATHS` (CSRF und Herkunft, GET 405). Knopf „Stornieren …“ nur bei Brutto > 0, mit Rückfrage und Stornodatum
  (vorbelegt heute, nicht vor Rechnungsdatum, nicht nach heute).
- **Stornorechnung:** Nummer aus dem Kreis des **Jahres des Stornodatums** (`allocate_invoice_number`), Kopf der Ursprungsrechnung (auch Leistungszeitraum und
  Zahlungsart) ohne Fälligkeit und Skonto, `PaymentTerms` = `STORNO_PAYMENT_TEXT`, `InvoiceKind='storno'`, `CancelsInvoiceId`, Status `sent` mit `SentAt`,
  **Summen exakt negiert** (Minor Units × −1, nichts neu gerechnet), Positionen mit negativer Menge und positivem Einzelpreis,
  `AmountDue` = negatives Brutto. Die Ursprungsrechnung wird `cancelled`.
- Alles in EINER Transaktion (`BEGIN IMMEDIATE`: Prüfung, Nummer, Beleg, Positionen, Status, je eine Protokollzeile für beide Belege,
  Gegenbuchung, Neuberechnung) – ganz oder gar nicht, keine Nummer verbraucht. Konten der Forderung vorher in eigenen Verbindungen,
  erst wenn sich die Rechnung stornieren lässt.
- Abgelehnt mit Klartext, nichts geschrieben: Stornodatum kein ISO-Datum, vor dem Rechnungsdatum oder **in der Zukunft** (400;
  maßgeblich `_today()`, Ortsdatum des Servers – sonst entstünde ein Nummernkreis 2062; nur Demodaten übergeben `today`); unbekannte
  ID/Angebot (404); Stornorechnung, Entwurf/abgeschlossen, schon storniert mit Stornorechnung, unbekannter Status, Brutto ≤ 0, schon
  eine Stornorechnung (409; eindeutiger Index auf `CancelsInvoiceId` zusätzlich). Verweis ins Leere (`ForeignKeyError`): Klartext, 409.
- **Die Forderung wird nie gelöscht:** Hat die App sie gebucht (`_is_app_receivable`, wie `_receivable_removal`, nur ohne Rücksicht auf
  Bankbewegung), bucht die Stornierung die **Gegenbuchung** („Debitoren an Wartekonto“ über `_book_receivable`, Form `transfer`,
  negativer Betrag und Steuer, Datum und Beleg-Nr. der Stornorechnung – für `receivable_condition` die Forderung einer Gutschrift);
  sonst (Import, Satz ohne Wartekonto, importierte/von Hand gebuchte Forderung) keine, Antwort trägt `hint`. Beleg mit Steuersatz je
  Position: je Satzgruppe.
- Zahlungen bleiben an der stornierten Rechnung. **Guthaben = Σ `InvoicePayments.Amount` über beide Belege des Paares**
  (`get_storno_credits`, `get_storno_credit`, `get_storno_credits_for_contact`, lesend); angezeigt wird das verbleibende
  (Rechnungsliste, beide Belegseiten, „Guthaben aus stornierten Rechnungen“ bei den offenen Posten des Kontakts: `storno_credits`, der
  Saldo zieht es ab).

### Erstattung (`Database.assign_storno_refund`, `db/invoice_payments.py`)

- Weg `POST /invoice/link-payment` mit der ID der Stornorechnung (`handle_link_invoice_payment` erkennt sie). Eine ausgehende
  Bankbewegung (Betrag < 0, Bankkonto mit SKR-Nummer) gleicht das Guthaben aus, zugeordnet **an der Stornorechnung** mit negativem
  Betrag; erstattet min(Guthaben, freier Rest, gewünschter Betrag auf Cent).
- Sätze (`payment_rows`, negativ): Datum, Steuerdatum, Empfänger, Währung der Bewegung, **Nummer der Stornorechnung** als Beleg-Nr.,
  Marke `invoice_payment`. Mit Gegenbuchung (`_receivable_sides`): „Bank an Debitoren“ + „Wartekonto an Erlöskonto“, sonst Einzelsatz
  „Erlöskonto an Bank“. Die folgenden Regeln zu Steuer und Erlöskonto gelten für den Beleg mit EINEM Satz.
- **Steuer:** nimmt zurück, was die Zahlungen an der stornierten Rechnung gebucht haben (`_allocations_tax`); gleicht die Erstattung das
  Guthaben aus: der Rest (Steuer der Zahlungen minus frühere Erstattungen), sonst anteilig (Cent, halb auf, nie mehr als der Rest).
  Ändern sich die Zahlungen nach der ersten Erstattung, kann die Summe um Cent abweichen.
- **Erlöskonto:** das der Zahlungen (`_payment_revenue_coa` über die markierten Sätze), wenn alle dasselbe tragen; bei **mehr als einem**
  (`refund_revenue_coas_differ`) wählt der Nutzer (`revenue_coa_id`, ein Erlöskonto, sonst Ablehnung; legt kein Konto an). Ohne
  markierte Sätze (Import) das für den Satz hinterlegte, sonst wählt der Nutzer. Erstattungssätze lehren kein Erlöskonto.
- Vorprüfung vor der Transaktion (eine abgelehnte Erstattung legt kein Konto an), Prüfung frisch in ihr; danach `_recalc_invoice`
  (Status der Stornorechnung ändert sich nie). Die Bankzeile bekommt nur den Kunden nachgetragen (Route, nicht DB-Schicht).
- **Entfernen** wie jede Zuordnung: Forderung und Gegenbuchung bleiben, das Guthaben ist wieder da. Entfernen einer **Erstattung** bleibt
  auf allen Wegen frei.
- **Das Guthaben wird auf keinem Weg der Oberfläche negativ:** Jeder Weg, der eine **Zahlung** an einer stornierten Rechnung mit
  Stornorechnung entfernt, fragt vor dem ersten Schreiben in seiner Transaktion `_refund_removal_blocker`: bliebe von den Zuordnungen
  beider Belege weniger als 0, lehnt `PaymentRemovalRefused` ab.
  - **Paar mit Steuersatz je Position:** zusätzlich je Satz (mehr Betrag/Steuer erstattet als gezahlt, Satz ganz erstattet ohne ganze
    Steuer, Satz ohne Zahlung erstattet): `MIXED_REMOVAL_REFUSED` mit Satz und Nummer der Stornorechnung – genau der Stand, in dem
    `split_payment` die nächste Erstattung ablehnte (`tax_groups.booked_fault`, `refund_pool`; `_mixed_booked` ohne die entfernten
    Zuordnungen: `_unbalanced_rate_after_removal`). Gilt, wenn die stornierte ODER die Stornorechnung das Kennzeichen trägt; nicht
    lesbarer Stand (`MixedNotBookable`): nur die Summe.
  - Wege: Rechnungsseite (`unassign_invoice_payment`, 409), ✕ im Bank-Editor, `remove_assignment_for_child`, `delete_transaction`,
    `/transactions/delete`. `_unassign` selbst prüft nichts (der Umzug braucht es so).
  - Der Editor prüft **alle** entfernten Zeilen zusammen beim Planen (`_plan_split_rows` → `Database.split_removal_blocker`, lesend, ein
    nur abgehängter Satz zählt nicht): 400 vor dem ersten Schreiben. Das Speichern des Editors ist EINE Transaktion (`save_bank_with_rows`,
    `buchungen.md`), die Einzelprüfung bleibt zusätzlich. Die Löschroute leitet mit Grund auf `/transactions?err=`.
  - Prüfliste: `refund_exceeds_credit` für Eingriffe an der App vorbei (bei Paar mit Steuersatz je Position dazu `rate_refund_mismatch`).
- Die Seite der Stornorechnung zeigt den Kasten „Erstattung“. Die EÜR nimmt den Erlös am Tag der Erstattung zurück, das Dashboard zählt
  die Bankbewegung als Ausgabe.

### Stornorechnung: Sperre und Dokument

- **Der Stornorechnung lässt sich keine Zahlung zuordnen** (`payment_blocker`, `assign_invoice_payment`, `link_invoice_to_transaction`;
  nie Kandidat, nie offener/überfälliger Posten – ihre Zuordnung ist allein die Erstattung); die Zahlungslogik ändert ihren Status nie.
- **Sie ist ganz gesperrt:** Status (409), Speichern, Berichtigen, Löschen, Kopieren – jede Abweichung: „Stornorechnung: Sie lässt sich
  nicht ändern.“, unverändertes Speichern ändert nichts. Seite schreibgeschützt, nennt die Ursprungsrechnung. Rechnungsliste: Status
  „Stornorechnung“, negative Beträge, in keiner Statistik-Summe.
- **Dokument:** Typ 381 – CII mit `ram:InvoiceReferencedDocument` (BT-25/BT-26) und BT-22 „Storno zu Rechnung … vom …“; UBL `CreditNote`
  mit `cac:BillingReference`, `cbc:CreditedQuantity`, ohne `cbc:DueDate` (`export/xrechnung_invoice.py`). Im XML **positive** Beträge
  (das Modell negiert genau einmal), im PDF die gespeicherten negativen, Titel „Stornorechnung“; kein Berichtigungs-Hinweis. Das
  Dokument der Ursprungsrechnung bleibt unangetastet. Erzeugt nach dem Commit (`handle_invoice_cancel`; Fehler lässt die Stornierung
  stehen, `warning`). Der E-Mail-Versand nennt sie „Stornorechnung“ und fordert nichts an.

### Neu ausstellen (`Database.create_replacement_draft`)

- `POST /invoice/reissue`, JSON `{"invoiceId"}`, in `_JSON_PATHS` und `_POST_ONLY_JSON_PATHS`. „Stornieren und neu ausstellen …“ schickt
  `reissue: true` an `/invoice/cancel`; ein Wert, der kein Wahrheitswert ist, ist eine ungültige Anfrage, bevor etwas geschieht.
- Zu einer stornierten Rechnung **mit** Stornorechnung entsteht ein Entwurf: Kopf und Positionen der stornierten (positiv, nichts neu
  gerechnet; Leistungsdatum und -zeitraum, Zahlungsart, Zahlungshinweis, Skonto übernommen), Nummer aus dem Kreis des Jahres von heute (`_today()`), heutiges Datum,
  Fälligkeit um dasselbe Zahlungsziel verschoben (sonst leer), Status `draft`, `Invoices.ReplacesInvoiceId`; kein Protokolleintrag.
- **Höchstens eine Ersatzrechnung je stornierter Rechnung** (Prüfung in der `BEGIN IMMEDIATE`-Transaktion, kein eindeutiger Index).
  Abgelehnt, nichts geschrieben, kein Zähler verbraucht: unbekannte ID/Angebot (404); Stornorechnung, nicht stornierte, stornierte ohne
  Stornorechnung, schon ersetzte, Kreis ohne brauchbare Nummer, Speicherfehler (409).
- „Stornieren und neu ausstellen“: Der Entwurf ist eine zweite Transaktion; scheitert er, bleibt die Stornierung stehen, `warning`.
  „Als neu kopieren“ (`copy_from`) setzt nie `ReplacesInvoiceId`.
- **Dokument:** „Ersetzt die stornierte Rechnung … (Stornorechnung …)“ (PDF fette Zeile vor der Tabelle; CII BT-22; UBL `cbc:Note`;
  TypeCode 380) aus dem Datenbankstand (`replacement_note`), nicht aus einem Parameter.
- **Nachträglich stornieren:** Von Hand stornierte Rechnung ohne Stornorechnung mit Brutto > 0: „Stornorechnung nachträglich erstellen
  …“ (`cancel_invoice` auf Status `cancelled`: Rechnung bleibt mit Status und Protokoll; Gegenbuchung nur bei stehender Forderung der
  App, sonst `hint`; Prüfliste `legacy_cancelled_open`). Danach kein Wiederbeleben.

## Nummern

- **Rechnungsnummer beim ersten Speichern** (`allocate_invoice_number`, `release_invoice_number`, `peek_next_number` in
  `db/numbering.py`): vergeben in der Transaktion des Speicherns (auch Entwurf, Angebot → Rechnung); Zähler rückt dort vor (`UPDATE` +
  `SELECT` in `BEGIN IMMEDIATE`, kein `RETURNING`: ältere SQLite-Versionen); scheitert das Speichern, ist sie nicht verbraucht.
- Der Vorschlag der Maske ist genau diese Nummer. **Die Nummer im Feld zählt nur als Vorschlag, solange sie unverändert bleibt:** Die
  Maske schickt ihn mit (`numberSuggestion`); ist sie beim Speichern genau er, vergibt der Server frisch und nennt sie in der Antwort
  (`invoice_number`).
- **Vergeben wird nach dem Jahr des Rechnungsdatums:** Fehlt der Kreis, entsteht er (Buchstabe, Zusatz, Vorlage des jüngsten). Eine
  eigene Nummer, die zur Vorlage eines Kreises passt, hebt dessen Zähler, nie nach unten. Statuswechsel und spätere Änderungen zählen nie.
- **War die Rechnung schon einmal versendet** (`Database.was_sent_before`), ist die Nummer auch im Entwurf gesperrt (409; Feld
  `readonly`; eine nur durch Leerraum abweichende Nummer bleibt unberührt).
- `invoiceId` liest `/invoice/save` als Ganzzahl (`"1"` und `1.0` gelten; `"01"`, `1.5`, `true` sind 400): Der Ordner `Fassungen/<ID>`
  heißt nach der Zahl.
- Eine Formatvorlage braucht **genau einen Zähler** (`{n}`, `{nn}`, `{nnn}`, `number_format_error`): sonst Ablehnung beim Speichern eines
  Kreises; ein Altbestand mit anderer Vorlage lässt den Vorschlag leer (`peek_next_number` wirft nie) und das Speichern mit
  `NumberRangeError` scheitern, nie die Seite.
- **Angebote auf demselben Weg** (`allocate_document_number(conn, 'quote', Datum, requested)`; `allocate_invoice_number` ist dieselbe
  Funktion für `'invoice'`): Kreis der Art `quote` entsteht beim ersten Speichern (Buchstabe `A`, `DOCUMENT_LETTERS`). Vorschlag
  (`peek_next_number('quote', Jahr)`) = die vom Speichern vergebene Nummer (auch ohne Kreis des Jahres); `/quote/save` antwortet
  `{success, quote_id}`. Löschen eines Angebots gibt die Nummer nicht zurück, Ändern fasst den Zähler nie an.
  `tests/test_quote_numbering.py`.

## Entwurf löschen, Angebot umwandeln

- **Entwurf löschen** (`POST /invoice/delete?id=`, `Database.delete_draft_invoice`): nur `draft`, ohne zugeordnete Zahlung und Forderung,
  in einer Transaktion; Ablehnung mit Grund (`err`). Ein Quellangebot `converted` ohne weitere Rechnung geht auf den Status vor der Umwandlung zurück (`Invoices.QuoteStatusBefore` des Entwurfs, gelesen über
  `restored_quote_status`; Bestand und Unlesbares: `accepted`). PDF/XML
  räumt `handlers.handle_delete_invoice` danach weg – nur im Dokumentordner, nur wenn kein anderer Beleg darauf verweist, nie in
  „Fassungen“ (`remove_document_file`). Ein abgeschlossener Entwurf wird erst auf Entwurf zurückgesetzt.
- **Angebot → Rechnung** (`convert_quote_to_invoice`): einmal je Angebot, nur aus Entwurf/versendet/angenommen (`UPDATE … WHERE Status IN
  (…)` in einer Transaktion). Ein Angebot **bleibt „umgewandelt“, solange es eine Rechnung daraus gibt**: `/quote/status`
  (`set_quote_status`) und `/quote/save` (`QUOTE_STAYS_CONVERTED_SQL`) lassen den Status nicht verlassen.

## Fassungen (`export/invoice_files.py`)

- Geht eine versendete Rechnung zurück auf Entwurf/abgeschlossen oder wird berichtigt, **verschiebt** `archive_sent_documents` PDF und
  XML nach `invoices/<Jahr>/Fassungen/<ID>/<bisheriger Name> – versendet bis <JJJJ-MM-TT>.<pdf|xml>`:
  - nie überschreiben (Zähler am vergebenen Namen), nie löschen; nur Dateien im Dokumentordner, die kein anderer Beleg führt; was schon
    in „Fassungen“ liegt, wird nie weiterverschoben; zeigt der gespeicherte Pfad woandershin, wird zusätzlich die Datei am Schema
    abgelegt; `PDFPath`/`XMLPath` geleert.
  - Zurückholen: `DocumentVersion` +1 (nur wenn eine Datei abgelegt wurde); `InvoiceDocumentVersions` hält Fassung, Pfad, SHA-256 (Anlass
    `reopened`, beim Berichtigen `corrected`).
  - Scheitert ein Verschieben: Verschobenes zurück, Zurückholen abgelehnt (auch bei scheiterndem Commit). Erneutes Versenden erzeugt
    eine neue Datei am Schema; die Fassung bleibt.
- **„War schon versendet“** (`Database.was_sent_before`, auch in einer Transaktion): `SentAt` gesetzt, ODER das Protokoll kennt einen
  Statuswechsel aus einem versendeten Status, ODER eine Datei liegt in `Fassungen/<ID>` in irgendeinem Jahresordner
  (`export.invoice_files.was_sent_before`). `delete_draft_invoice` gibt die Nummer dann **nie** frei (Lücke bleibt, auch ohne abgelegte
  Datei), die Rückfrage beim Löschen sagt es.
- Der Ordner liegt unter `invoices/` und ist Teil von Sicherung und Wiederherstellung (`tests/test_invoice_versions.py`).
