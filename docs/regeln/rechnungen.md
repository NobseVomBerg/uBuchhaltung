# Rechnungen und Angebote: Dokumente, Steuer, Sperre, Storno, Nummern

## Rechnungsdokumente (PDF + XRechnung)

- Ablage: `<Datenordner>/invoices/<Jahr des Rechnungsdatums>/<Rechnungs-Nr.> <Kunde>.pdf`
  (z. B. `R26003 Malermeister Pinsel.pdf`); die XRechnung trägt denselben Namen
  mit `.xml`. Namensschema und Ablage liegen zentral in `export/invoice_files.py`
- **Hybride E-Rechnung (ZUGFeRD 2.x / Factur-X, Profil EN 16931):** jede neu
  erzeugte Rechnungs-PDF ist PDF/A-3b mit eingebetteter `factur-x.xml` (CII,
  `/AFRelationship /Alternative`). Datenfluss: DB → `export/einvoice_model.py`
  (neutrales Modell, Beträge in Minor Units, Steueraufschlüsselung als Liste) →
  `cii_invoice.py` (eingebettet) und `xrechnung_invoice.py` (UBL, Download).
  Der PDF/A-Aufbau liegt in `export/pdfa.py` und gilt für **alle** PDFs
  (Angebot, Stundenzettel …); nur Rechnungen bekommen das XML
- **Steuer je Satzgruppe (`tax_groups.py`, seit 2.6.8):** `rate_groups` ist die EINE Stelle, die die Steuer eines Belegs je Satz rechnet (rein:
  nur `money`, keine Datenbank, keine Uhr; für beliebig viele und beliebige Sätze – nie „19 und 7“ fest): Steuer je Satz aus der Summe der
  Nettobeträge der Gruppe, kaufmännisch auf den Cent gerundet (EN 16931, BR-CO-17), Belegsteuer = Summe der Gruppen, Satz absteigend.
  `handlers.recompute_invoice_totals` (Summen beim Speichern) und `einvoice_model.build_einvoice` (die Steuer je Gruppe der Aufschlüsselung; die
  Gruppe des Dokuments bleibt (Kategorie, Satz)) rechnen darüber. `rate_pct` ist die Regel von `einvoice_model.rate_to_pct` ohne Vorgabewert;
  `handlers._rate_to_pct` ist davon bewusst nicht abgeleitet (sie rundet nicht auf zwei Stellen) – jede Stelle behält ihre Zahlen.
  `tests/test_single_rate_snapshots.py` hält am Stand 2.6.6 fest, was ein Beleg mit einem Satz bucht und ausgibt – ändert sich ein
  Schnappschuss, ist der Code zu ändern, nie die Datei (`tests/single_rate_golden_2_6_6.json`); `tests/test_tax_rate_readers.py` führt die
  Lesestellen des Belegsteuersatzes (eine neue schlägt an: prüfen, ob sie mit einem Beleg mit mehreren Sätzen richtig umgeht);
  `tests/test_tax_groups.py`
- **Mehrere Steuersätze je Beleg (seit 2.7.0, Rechnung wie Angebot):** Das Kennzeichen `Invoices.MixedTaxRates` (NULL = ein Satz für den ganzen Beleg – der
  ganze Bestand –, `1` = Satz je Position) liest EINE Funktion streng: `db.invoices.has_mixed_rates` (nur die ganze Zahl 1 ist wahr; sie steht in
  `db/invoice_payments.py`, dem untersten der Rechnungsmodule, `db.invoices` reicht sie weiter). Geschrieben wird nur NULL oder 1 (`insert_invoice`, sonst
  `ValueError`). **Der Satz im Kopf (`Invoices.TaxRate`) ist bei `MixedTaxRates = 1` nur ein Platzhalter: der höchste Satz der Positionen als Bruch (0,19)** – nie
  negativ, die Prüfungen auf § 19 (`TaxRate` < 0) bleiben richtig; was rechnet oder bucht, nimmt die Satzgruppen (`tax_groups.rate_groups`), nie den Kopfsatz für den
  ganzen Beleg. **Speichern prüft EINE Funktion für beide Wege** (`handlers._tax_fields`; **verglichen wird der Satz, mit dem gerechnet wird** – `_exact_rate`, ungerundet: 19,004 ist nicht 19, ein gerundeter Vergleich ließe Summen
  entstehen, die nicht zum geführten Satz passen –, gespeichert wird, wie das Formular ihn schickt): `taxMode` fehlt oder ist leer (das Formular ohne Häkchen „Steuersatz je Position“) oder ist `'mixed'`, jeder andere Wert ist
  ungültig; dann drei Regeln in dieser Reihenfolge – (1) ohne Steuerausweis (`showTax` falsch) wird `'mixed'` abgelehnt, sonst ist der Kopf −1 und jede Position bekommt den Satz 0;
  (2) mit Steuerausweis ohne `taxMode` ist der Kopf eine Zahl von 0 bis 100 Prozent (nie negativ: −1 ist der Platzhalter von Regel 1), alle Positionen tragen GENAU denselben Satz, und er ist
  der des Kopfs (bis auf das Rauschen der Kommazahl – die Maske schickt den Kopf als Bruch, 100 % als 1); ein Satz zwischen 0 und 1 Prozent lässt sich nicht eindeutig lesen und wird abgelehnt
  (2.6.x rechnete dort still mit dem Hundertfachen) – für jeden anderen Satz des Formulars von heute ändert sich in der Datenbank nichts;
  (3) mit `'mixed'` braucht jede Position GENAU einen Satz aus `tax_groups.MIXED_TAX_RATES` (19 und 7 % – die Sätze, die ohne Eintrag der Stammdaten gelten; seit 2.8.0 daneben jeder wählbare Eintrag der Stammdaten über `taxRateId`, siehe *Steuersätze aus den Stammdaten*; kein Code nimmt „zwei Sätze“ oder „19 und 7“
  fest an): alle gleich ist ein gewöhnlicher Beleg (Kopf = dieser Satz, Kennzeichen NULL), sonst ist der Kopf der höchste Satz und das Kennzeichen 1. Jede Ablehnung hat einen festen Klartext
  (`TAX_*` in `handlers.py`) und kommt vor dem ersten Schreiben (nichts gespeichert, keine Nummer verbraucht). Eine versendete Rechnung schickt keine Positionen; der Satz einer Position
  ist gesperrt (`_items_differ`), das Kennzeichen ein abgeleitetes, nie für sich verglichenes Feld (`invoice_lock.FIELDS`). Angebot → Rechnung (`convert_quote_to_invoice`),
  Stornorechnung und Ersatzrechnung (`_copied_head`) übernehmen das Kennzeichen zu den Sätzen der Positionen. **Buchen (seit Etappe 2a und 2b): Forderung, Zahlung, Stornieren und Erstattung je Satz; der Neuaufbau rechnet nur den offenen Betrag, der Umzug der Zahlungen unterbleibt, ein Weg lehnt ab.**
  Die Gruppen eines Belegs liest `Database._mixed_groups` (`tax_groups.mixed_groups`: aus Positionen und gespeicherten Summen, auf den Cent gleich den Summen – sonst `MIXED_GROUPS_MISMATCH`; der
  Platzhalter −1, jeder negative, unlesbare oder über 100 liegende Satz und ein Nettobetrag, der keine ganze Zahl ist, gelten nicht), zum Buchen geprüft von `_checked_mixed_groups` (jede Gruppe mit positivem Brutto,
  sonst `MIXED_GROUP_NOT_POSITIVE` mit Satz und Betrag; `MixedNotBookable` ist ein `ValueError` mit dem festen Klartext – die Gründe im Detail nur im Server-Log). Der Satz einer Gruppe steht in
  `Bookings.TaxRate` in EINER Form (`tax_groups.booking_rate`: 0,19 / 0,07 / 0,055, 0 % = NULL – dieselbe Zahl wie beim Beleg mit einem Satz, damit jede Stelle, die einen Satz liest, ihn gleich liest).
  **Versenden und Wiederbeleben** buchen je Gruppe eine Forderungszeile „10000 an Wartekonto“ (`_book_mixed_receivable`: 4405 für 19 %, 4345 für 7 %; das Wartekonto je Satz nur bei GENAU diesem Satz,
  `_group_waiting_entry` – nie über `_tax_rate_key`, der auf ganze Prozent rundet; fehlt einer Gruppe das Wartekonto, wird gar keine Forderung gebucht, und jede Zahlung bucht je Satz „Erlöskonto an Bank“),
  die Konten löst `_mixed_receivable_accounts` vor der Transaktion auf (erst wenn jede Gruppe buchbar ist) und die Transaktion vergleicht den Stand der Gruppen mit der Vorbereitung (Schlüssel
  `('mixed', Gruppen)`, sonst `CHANGED_MESSAGE`, 409; auch `ensure_receivable_booking`). **Zahlung zuordnen** (`_assign_in_tx` → `_assign_mixed`, nur über das Kennzeichen; Kennzeichen und
  Kontext (`mixed`) müssen zusammenpassen, sonst der „geändert“-Satz – das Netz für Neuaufbau und Umzug, deren Kontext kein `mixed` trägt: Sie buchen für so einen Beleg nie je Satz und kommen nicht bis hierher, ein Fehler an
  ihnen vorbei lehnt hier ab): „Bank an Debitoren“ über den ganzen
  Betrag und je Satz „Wartekonto an Erlöskonto“ mit Satz und Steuer des Anteils (`payment_rows(parts=…)`), ohne Forderung je Satz „Erlöskonto an Bank“. Den Anteil je Satz rechnet die reine Funktion
  `tax_groups.split_payment`: **Betrag** – die schließende Zahlung schließt jede Gruppe mit ihrem Rest, jede andere zielt je Satz auf den Anteil am Brutto, den die Summe der Zahlungen danach erreicht (auf
  den Cent, **bei genau einem halben Cent nach unten**), und nimmt davon, was der Satz noch braucht; der Cent-Rest geht an die Gruppe mit dem größten offenen Betrag (bei Gleichstand der höhere Satz),
  ein Überhang oder Fehlbetrag in der Reihenfolge der Gruppen an die anderen; **Steuer** – schließt ein Teil die Gruppe, bekommt er den Rest ihrer Steuer, sonst den anteiligen Betrag, höchstens den Rest
  (nie eine Zeile mit Betrag 0 und Steuer). Eingabe: was die anderen Zuordnungen je Satz tragen (`_mixed_booked` liest die markierten Sätze; eine Zuordnung ohne sie – Import, von Hand –, ein fremder Satz
  oder eine Summe, die nicht aufgeht, lehnt mit `MIXED_ALLOCATION_UNREADABLE` ab: die App rät nicht) und die Forderungen (`_receivable_sides_by_rate`: genau die Sätze des Belegs, sonst
  `MIXED_RECEIVABLE_MISMATCH`). **Das Erlöskonto je Satz** ergibt `_group_revenue_coa` (`resolve_revenue_coa` über `booking_rate`; ein Satz von 0 % hat bewusst keines, nie 4184/4185); der Nutzer wählt keines
  (`revenue_coa_choice(mixed=…)` liefert nichts, `MIXED_NO_REVENUE_COA` lehnt ab, Zahlungskasten und Buchungsmaske zeigen keine Wahl), und `resolve_revenue_coa` lernt es aus dem Satz des Buchungssatzes
  (`_learned_revenue_coa`: bei dem Kennzeichen `Bookings.TaxRate`, nie der Kopf – sonst lernte 19 % das Konto der 7 %). Die Bankzeile bekommt nach der Zuordnung nur Kunde und Beleg-Nr.
  (`_backfill_booking_skr`). Entfernen nimmt die ganze Zuordnung mit (auch das ✕ im Bank-Editor), Stufe 7 des Abgleichs löst die Forderungszeilen gemeinsam auf (*Rechnungszahlung*). Die Auswertungen (EÜR,
  Summen- und Saldenliste, DATEV-Stapel, Dashboard, Liquidität, Prüfliste) bleiben **ohne Änderung** und gehen auf den Minor-Wert auf (`tests/test_mixed_tax_reports.py`). **Stornieren** (`cancel_invoice`, Etappe 2b):
  Die Vorprüfung liest die Gruppen (`_checked_mixed_groups`; ein Beleg, der sich nicht buchen lässt, lehnt mit `MIXED_GROUPS_MISMATCH` bzw. `MIXED_GROUP_NOT_POSITIVE` ab – 409, vor dem Auflösen der Konten,
  nichts angelegt), die Konten gelten für seine Satzgruppen (`_mixed_receivable_accounts`, Schlüssel `('mixed', Gruppen)`), und die Transaktion vergleicht den frisch gelesenen Stand mit ihm (`CHANGED_MESSAGE`).
  Die Stornorechnung trägt das Kennzeichen und die negierten Positionen und Summen; ihre Gruppen sind auf den Cent die negierten der Rechnung (`round_minor` rundet halb von der Null weg, also symmetrisch).
  Die Gegenbuchung – nur, wenn die Forderungen der App stehen, wie bei einem Beleg mit einem Satz – bucht je Gruppe eine Zeile „10000 an Wartekonto“ mit negativem Betrag, negativer Steuer und dem Satz der Gruppe
  (Nummer und Datum der Stornorechnung); ergeben die Positionen der Stornorechnung nicht ihre Summen (nur nach einem Eingriff an der App vorbei), lehnt `MIXED_GROUPS_MISMATCH` ab und nimmt alles zurück, keine Nummer
  ist verbraucht. **Erstattung** (`assign_storno_refund`, `_refund_plan` → `_mixed_refund_parts`; trägt nur EINER der beiden Belege das Kennzeichen, lehnt `MIXED_GROUPS_MISMATCH` ab): Den Betrag verteilt
  `tax_groups.split_payment` – der Topf ist je Satz das, was die Zahlungen der stornierten Rechnung getragen haben (`_mixed_booked`), nicht das Brutto der Gruppe (eine Teilzahlung hat es noch nicht gefüllt), die
  früheren Erstattungen (die Zuordnungen der Stornorechnung, Vorzeichen gedreht) zählen als gebucht, und die Erstattung, die das Guthaben ganz ausgleicht, nimmt von jedem Satz den ganzen Rest samt Steuer zurück;
  gebucht wird „Bank an Debitoren“ über den ganzen Betrag und je Satz „Wartekonto an Erlöskonto“ (alles negativ, ohne Gegenbuchung je Satz „Erlöskonto an Bank“; die Wartekonten sind die der Gegenbuchung, `MIXED_RECEIVABLE_MISMATCH`,
  wenn sie nicht genau die Sätze des Belegs trägt). Das Erlöskonto je Satz ist das, auf dem die Zahlungen dieses Satzes liegen (`_refund_revenue_coas_by_rate`); liegen sie auf mehr als einem oder ist es kein Erlöskonto,
  lehnt `MIXED_REFUND_REVENUE_AMBIGUOUS` mit dem Satz ab – der Nutzer wählt keines (`revenue_coa_id` gilt nie, `refund_revenue_coas_differ` ist für so eine Stornorechnung immer falsch, die Seite zeigt keine Auswahl).
  Geht die Aufteilung nicht auf (ein Satz hat mehr erstattet, als die Zahlungen hergeben – eine Zahlung wurde nach einer Erstattung entfernt oder von Hand geändert), lehnt `MIXED_REFUND_SPLIT` ab, eine Zuordnung ohne die Sätze
  der App `MIXED_ALLOCATION_UNREADABLE`: die App rät nicht. In der Transaktion wird neu geplant und mit der Vorprüfung verglichen (Teile je Satz samt Erlöskonten, Bankkonto der Bewegung; sonst „geändert“).
  Das Entfernen einer Zahlung schützt dasselbe Guthaben wie bei einem Satz (`_refund_removal_blocker`) – über alle Sätze und zusätzlich je Satz (`MIXED_REMOVAL_REFUSED`, siehe *Stornobeleg*). **Neu ausstellen und Umzug:** `create_replacement_draft` kopiert das Kennzeichen; die Zahlungen der
  stornierten Rechnung ziehen nicht auf die Ersatzrechnung um, wenn die stornierte ODER die neue Rechnung einen Steuersatz je Position trägt (`_move_replaced_payments`: Der Umzug vergleicht die Sätze der App mit
  `payment_rows` EINES Satzes und bucht mit EINEM Erlöskonto neu – je Satz zu verallgemeinern ist Etappe 3 und rührte an einer Regel, die nicht gelockert wird); der Hinweis ist `MOVE_MIXED_HINT` mit der Nummer
  der stornierten Rechnung und ihrem Guthaben, die Zahlungen bleiben dort und lassen sich erstatten oder – entfernt – an der Ersatzrechnung neu zuordnen (nach einer Teilerstattung dazu „… sind bereits erstattet …“, `MOVE_REFUNDED_HINT`;
  ist alles erstattet, sind es dieselben Sätze wie beim Beleg mit einem Satz, `MOVE_NONE_HINT` und `MOVE_REFUNDED_HINT`, ohne den Halbsatz zum Erstatten und Neu-Zuordnen; ohne Zahlung gibt es keinen Hinweis;
  `_payment_move_context` löst für den Platzhalter im Kopf nie ein Erlöskonto auf). **Zuordnung neu aufbauen:** nur der offene Betrag (Grund `mixed`, siehe dort). **Prüfliste:** `rate_split_mismatch`, `rate_refund_mismatch` und
  `unflagged_rate_mix` (siehe *Rechnungszahlung*). **Ein Weg lehnt noch ab,
  mit EINEM Satz** (`MIXED_LINK_NOT_BOOKABLE`, VOR dem ersten Schreiben, am frisch in der Transaktion gelesenen Beleg; nichts geändert): `link_invoice_to_transaction` (der WISO-Import: schreibt eine Zuordnung ohne Sätze,
  deren Anteil je Satz keine spätere Zahlung, Erstattung oder Prüfung kennen könnte – seine Aufrufer legen nur Belege ohne das Kennzeichen an). Es gibt keinen allgemeinen Satz „lässt sich noch nicht buchen“ mehr.
  `create_replacement_draft` bucht nichts; `update_invoice_status` (der ungeprüfte Baustein für Seed und Tests) bleibt, wie er ist. Ein Weg, der einen Beleg mit Steuersatz je Position buchen soll, bucht je Satz oder lehnt
  ab – nie mit dem Kopfsatz. Tests: `tests/test_mixed_tax_storno.py`, `tests/test_mixed_tax_locks.py` (die eine Sperre und die Ein-Satz-Gegenstücke).
  **PDF (Rechnung, Stornorechnung, Angebot):** Nur mit dem Kennzeichen – nie nach den Sätzen der
  Positionen; ein Beleg ohne das Kennzeichen druckt Zeichen für Zeichen wie vorher (trägt er Positionen zu verschiedenen Sätzen, steht eine Zeile im Server-Log: `einvoice_model.log_unflagged_rates`) –
  hat die Tabelle zwischen Bezeichnung und Einzelpreis die Spalte „USt“ (`pdf_document.COL_RATE_R`; sie rückt nach links, wenn ein Einzelpreis mehr Platz braucht – `_rate_layout`, einmal je Beleg –, die
  Bezeichnung bricht früher um, ein Wort ohne Leerzeichen, das breiter ist als sie, mitten im Wort), und der Summenblock nennt unter „Summe netto“ je Satzgruppe eine Zeile „zzgl. 7 % MwSt. auf 50,00
  EUR:“ (je Gruppe um eine Zeile höher: Seitenumbruch; ab 46 Gruppen passt er auch auf eine eigene Seite nicht, dann brechen die Zeilen auf die Folgeseite um, der Gesamtbetrag nie allein). Der Satz im
  Kopf wird dort nie gedruckt (so ein Beleg ist nie § 19). Die Gruppen kommen aus `tax_groups.item_groups` (die EINE Stelle; die Buchungen nehmen dieselbe) über `einvoice_model.per_item_rates` und sind
  auf den Cent die der E-Rechnung und, summiert, die gespeicherten Summen des Belegs – weicht eine ab, druckt das PDF trotzdem, und das Server-Log nennt es. Beispiele für Mustang: `zugferd-mischsatz.pdf` und
  `xrechnung-mischsatz.xml` (`tools/make_einvoice_samples.py`; sein Normalfall ist nie ein Beleg mit dem Kennzeichen, seine Kopien nehmen es mit). **Maske (Rechnung und Angebot):** Das Häkchen
  „Steuersatz je Position“ (`#mixed_tax`, unter „Umsatzsteuer ausweisen“; nur mit Steuerausweis, in einer gesperrten Rechnung gesperrt, vorbelegt aus `has_mixed_rates`) schaltet die Spalte „USt“
  ein (Klasse `mixedTaxOn` an der Positionstabelle, `buch.css`) und im Summenblock eine Zeile je Satz (nur Anzeige, in ganzen Cent und zweistufig gerundet wie `money`; maßgeblich bleibt der Server).
  Die Auswahl kommt vom Server (über `script_json`), seit 2.8.0 aus den Einträgen der Stammdaten (`entry_choices(db)`; ohne Liste aus `tax_groups.MIXED_TAX_RATES`), im Skript steht kein Satz. Ohne Häkchen schickt das Speichern genau den bisherigen Payload, mit Häkchen
  zusätzlich `taxMode: 'mixed'` und je Position den Satz ihrer Auswahl. Ein gespeicherter Satz außerhalb der Liste erscheint als eigene, gewählte Option (nichts wird still umgedeutet, der Server lehnt
  beim Speichern ab); die Zeilen eines als gemischt geladenen Belegs behalten ihre Sätze, wenn das Häkchen aus- und wieder eingeschaltet wird. EIN Skriptblock für beide Masken (`MIXED_TAX_JS` in
  `server/pages_invoice.py`, nur `var`/`function`, Präfix `mix`).
  `tests/test_mixed_tax_save.py`, `tests/test_mixed_tax_locks.py`, `tests/test_mixed_tax_bookings.py`, `tests/test_mixed_tax_reports.py`, `tests/test_tax_groups_split.py`,
  `tests/test_mixed_tax_documents.py`, `tests/test_mixed_tax_mask.py`
- **Steuersätze aus den Stammdaten (seit 2.8.0, Schema 2008):** Weitere Sätze (5,5 % …) und steuerfreie Leistungen wählt eine Rechnung oder ein Angebot aus der
  Liste Stammdaten → Steuersätze (`TaxRates`, Seite `/masterdata/taxrates`, `db/tax_rates.py`: `TaxRatesMixin`, `TaxEntry`, `TaxRateRefused`; Handler im eigenen Abschnitt am
  Ende von `handlers.py`) – für den ganzen Beleg oder je Position, mit 19 % und 7 % mischbar. **Eintrag:** Bezeichnung, Satz in Hundertstel Prozent (`RateBp`: 1900 = 19 %,
  550 = 5,5 %; aus Text nur über `tax_groups.parse_rate_bp`, als Prozentzahl `bp_pct`), Art (`standard` oder `exempt` = steuerfrei, nur mit Satz 0 und Pflicht-Grund),
  Erlöskonto (Pflicht außer bei 19 % und 7 % ohne eigene Angabe: `has_default_revenue`), freiwillig Umsatzsteuer- und Vorsteuerkonto (nur 3800–3849 bzw. 1400–1439, nie bei
  Satz 0 und nie bei den Sätzen mit festen Steuerkonten 19/16/7/5 %, `FIXED_TAX_BPS`), Reihenfolge, „wählbar“. Vorbelegt sind 19 % und 7 % – genau einmal, beim Anlegen der
  Tabelle (aus `tax_groups.MIXED_TAX_RATES`); wer alle Einträge löscht, bekommt keinen zurück. **Die Regeln eines Eintrags stehen an EINER Stelle**
  (`tax_groups.check_tax_entry`, feste Sätze `TAX_ENTRY_*`; ein Satz zwischen 0 und 1 % gilt nicht, Bezeichnung und Grund ohne Steuer-/Formatzeichen) und gelten beim Schreiben wie
  beim Lesen (`TaxEntry.valid`; wählbar = gültig UND aktiv; eine ungültige Zeile aus der SQL-Konsole steht in keiner Auswahl, die Liste nennt sie „ungültig“). Der letzte wählbare
  Eintrag bleibt (`TAX_ENTRY_LAST_ACTIVE`); sonst lässt sich jeder Eintrag frei ändern und löschen – **kein Beleg verweist auf ihn**. Es gibt keinen Befreiungscode (BT-121).
  **Momentaufnahme:** Die Position trägt nie die ID, sondern den Stand des Eintrags beim Speichern: `InvoiceItems.TaxRateBp` (Index 11), `TaxExemptReason` (12),
  `RevenueCoaId` (13, ohne Fremdschlüssel; `coa_is_referenced` schützt das Konto) und `TaxCategory` (9: `'E'` = steuerfrei) – `db.invoice_payments.ITEM_SNAPSHOT_KEYS`,
  gebaut nur vom Server (`db.tax_rates.entry_snapshot`). **Das Merkmal ist `TaxRateBp IS NOT NULL`** – nie `'E'` allein (das § 19-Beispiel des Beispielwerkzeugs schreibt `'E'`
  ohne Momentaufnahme); gültig ist 0 oder 100 bis 10000 und gleich `rate_pct(TaxRate)` × 100 (`invoice_payments.valid_rate_bp`, `einvoice_model.item_bp` – ein Test hält beide
  gleich). Stornorechnung, Ersatzrechnung und Angebot → Rechnung kopieren alle vier Spalten; **jeder neue Kopierweg einer Position muss es auch tun**
  (`tests/test_tax_rates_schema_2008.py`). Änderungen der Stammdaten wirken nur auf neue und neu gespeicherte Entwürfe.
  **Speichern** (`handlers._tax_fields(db, data, items)`, EINE Stelle für Rechnung und Angebot): `taxRateId` im Kopf (nur ohne `taxMode`: der Eintrag gilt für alle Positionen,
  das Kennzeichen bleibt NULL – so entsteht ein ganz steuerfreier Beleg mit Kopf 0) oder je Position (nur mit `taxMode: 'mixed'`); der Eintrag muss jetzt wählbar sein, der Satz
  des Formulars der des Eintrags, `0` ist immer eine Ablehnung („gespeicherter Stand, Eintrag nicht mehr vorhanden“), ohne Steuerausweis gibt es keine ID (`TAX_CHOICE_*`).
  **Je Satz trägt ein Beleg höchstens EINE Momentaufnahme – auch „mit“ gegen „ohne“** (`TAX_SAME_RATE_TWICE`): nie zwei 0-%-Arten, nie zwei Befreiungsgründe, nie eine Gruppe mit
  gemischter Rundung; Dokumente und Buchen verlassen sich darauf – nicht lockern. Ein Payload ohne `taxRateId` wird gespeichert wie vor 2.8.0 (auch der freie Kopfsatz; ein Entwurf
  verliert dabei seine Momentaufnahmen). „Position n“ in den `TAX_*`-Meldungen ist `item['position']`. Die Sperre versendeter Rechnungen vergleicht die vier Spalten mit
  (`invoice_lock.stored_items`, `_items_differ`).
  **Maske (Rechnung und Angebot):** Im Kopf wählt `#tax_choice` (Klasse `taxChoice`) den Eintrag, mit dem Häkchen „Steuersatz je Position“ die Spalte „USt“ je Zeile; `#tax_rate` bleibt als
  verstecktes Feld mit der Prozentzahl (bei einer gesperrten Rechnung `disabled`), `#tax_choice` schreibt bei `change` den Satz hinein und ruft `calculateTotals()`. Die Liste ist
  `entry_choices(db)` (wählbare Einträge in der Reihenfolge der Stammdaten, als `MIX_RATES` über `script_json`; im Skript steht kein Satz, keine ID, keine Bezeichnung). Ein Eintrag, den
  der bisherige Payload trägt (`legacy_expressible`: Standard, ohne Erlöskonto, ganzzahliger Satz – die Vorbelegung), hat als Optionswert seinen Satz und schickt keine ID: **der Payload
  für 19 % und 7 % bleibt Zeichen für Zeichen der von 2.7.0** (`tests/test_mixed_tax_mask.py`); jeder andere hat den Wert `e<ID>` und schickt `taxRateId` als letzten Schlüssel des Kopfs
  bzw. der Position. **Ein geladener Beleg wählt den Eintrag nach seiner Momentaufnahme** (`entry_snapshot` gleich Art, Satz, Grund, Erlöskonto – auch bei „Als neu kopieren“, Angebot →
  Rechnung und Ersatzrechnung; nur ein umbenannter Eintrag bleibt derselbe); gibt es ihn nicht mehr, steht die gewählte Option `e0` „… – gespeichert, in den Stammdaten nicht mehr
  vorhanden“ da (`taxRateId: 0`, der Server lehnt ab, bis neu gewählt ist). Ohne Momentaufnahme gilt die Vorbelegung nach Satz, sonst der Satz als eigene Option ohne ID. Die Anzeige
  rechnet eine Gruppe, deren Zeilen alle einen Eintrag mit ID tragen, einstufig (maßgeblich bleibt der Server), eine steuerfreie Gruppe heißt „Steuerfrei auf … netto“, und der
  Befreiungsgrund des gewählten Eintrags steht als Text unter den Häkchen (`#tax_note`, `textContent`). Zwei Skriptkonstanten in EINEM Block (`MIXED_ENTRY_JS` vor `MIXED_TAX_JS`; die
  Funktionsinventur des alten Tests gilt `MIXED_TAX_JS`). `buch.css`: `.invoice-items .item-tax` wächst mit der längsten Bezeichnung (höchstens 130px), `.taxChoice` höchstens 260px.
  `tests/test_tax_entries_mask.py`
  **Rundung – zwei Regeln, eine Stelle** (`tax_groups.group_tax(net, pct, exact)`): Eine Satzgruppe, deren Positionen ALLE eine Momentaufnahme tragen, rechnet einstufig (Netto × Satz,
  EINMAL auf den Cent, halb weg von null – EN 16931, BR-CO-17); jede andere zweistufig wie vor 2.8.0, Bit für Bit. Beide sind gleich bei ganzzahligem Satz und Netto in ganzen Cent
  (bewiesen und aufgezählt in `tests/test_tax_entries_pure.py`); 5,5 % weicht an jedem Betrag mit Cent ≡ 9 (mod 200) ab (0,09 € → 0,00 statt 0,01 €). `rate_groups`, `item_groups`
  und `mixed_groups` nehmen dafür ein drittes Element (`exact` bzw. `bp`); `recompute_invoice_totals(items, default, snapshots)` rechnet die gespeicherten Summen so. Jede Stelle, die
  Gruppen aus Positionen liest, liest `TaxRateBp` mit – sonst laufen Summen, Dokument und Buchung auseinander. Sätze in Texten nur über `pct_text`/`group_label` (aus `bp_pct` ist
  10 % `Decimal('10')`, aus `rate_pct` `1E+1`).
  **Buchen:** Das Erlöskonto der Momentaufnahme gilt bei Zahlung und Erstattung immer – nie 4184/4185, nie ein gelerntes Konto, keine Wahl des Nutzers (`revenue_coa_choice(…, entry=…)`);
  eine Rechnung mit Stammdaten-Erlöskonto lehrt keines (`_learned_revenue_coa`). Ist es kein Erlöskonto (mehr), lehnt `TAX_PAYMENT_NO_REVENUE` ab; **das Versenden prüft es vorab**
  (`_entry_revenue_blocker`, 409 `TAX_SEND_NO_REVENUE`, vor dem Anlegen von Konten und vor dem ersten Schreiben – ein abgelehntes Versenden legt nie ein Konto an). **Wartekonten
  bleiben fest:** 19 % → 4405, 7 % → 4345, Satz 0 (steuerfrei wie 0 %) → 4340, und zwar genau (`_group_waiting_entry`, bei einer Momentaufnahme nie das gerundete `_tax_rate_key`);
  jeder andere Satz hat keines – der ganze Beleg bucht dann keine Forderung, jede Zahlung „Erlöskonto an Bank“ je Satz, und die Antwort des Versendens trägt `NO_WAITING_HINT`
  (ein Wartekonto je Eintrag gibt es bewusst nicht: EÜR, Summen- und Saldenliste, DATEV-Kette und Storno erkennen Wartekonten an festen Nummern). **Widersprüche der Momentaufnahme**
  (verschiedene Angaben in einer Gruppe, teils mit/teils ohne, halbe Angabe, `'E'` mit Satz ≠ 0, unlesbarer oder nicht passender Satz, Kopf passt nicht – nur an der App vorbei erreichbar)
  lehnen Versenden, Zahlung und Stornieren vor dem ersten Schreiben ab (`TAX_ENTRY_CONTRADICTION`; `MIXED_GROUPS_MISMATCH` bleibt für „Positionen ≠ Summen“); die **Erstattung prüft sie
  bewusst nicht** und folgt den Zahlungssätzen (ein Guthaben muss sich auch aus so einem Stand erstatten lassen). Der Umzug der Zahlungen auf die Ersatzrechnung unterbleibt, sobald
  eine der beiden Rechnungen ein Stammdaten-Erlöskonto trägt (`MOVE_ENTRY_HINT`), „Zuordnung neu aufbauen“ rechnet dann nur den offenen Betrag (Grund `tax_entry`) – die Bedingungen
  beider Wege sind nicht gelockert. Prüfliste: `tax_entry_mismatch` (feste Sätze `TAX_ENTRY_FINDINGS`, ohne Beträge; `'E'` ohne Angaben ist kein Befund, die halbe Angabe schon). In
  Meldungen heißt die steuerfreie Gruppe „steuerfreie Leistungen“ (`rate_sentence`), in Listen und im PDF „steuerfrei“ (`group_label`).
  **Dokumente:** Satz der Position aus der Momentaufnahme; steuerfrei ist Kategorie E mit dem Befreiungsgrund als BT-120 – aus EINER Funktion für PDF und XML
  (`einvoice_model.exempt_reason`: erster nicht leerer Grund der steuerfreien Positionen, bereinigt, höchstens `MAX_REASON_LENGTH` Zeichen plus „…“), nie der § 19-Text; fehlt er, meldet
  `einvoice_gaps` „Grund der Steuerbefreiung“ und das UBL entsteht nicht. PDF: Summenzeile „steuerfrei auf 80,00 EUR:“ für eine ganz steuerfreie 0-%-Gruppe, der Grund unter dem
  Summenblock (gesetzt mit den Breiten der eingebetteten Schrift, höchstens 12 Zeilen), ein Beleg ganz mit einem Stammdaten-Satz druckt „zzgl. 5,5% MwSt.:“ mit Komma. Ein Beleg OHNE
  Momentaufnahme ergibt Byte für Byte dieselben Dokumente wie vor 2.8.0. Beispiele für die Prüfung: `zugferd-satz-5-5`, `zugferd-steuerfrei-gemischt`, `zugferd-steuerfrei`
  (`tools/make_einvoice_samples.py`; der Validator nimmt auch die zweistufig gerundete Steuer an – die Einstufig-Regel hält `tests/test_tax_entries_demo_samples.py`).
  **Auswertungen:** siehe *EÜR-Ansicht* und *Summen- und Saldenliste* (Steuerkonten der Stammdaten, Zeile „Vorsteuer ohne Steuerkonto“). DATEV: Ein Satz außerhalb von 19/16/7/5 %
  bekommt keinen BU-Schlüssel (Schlüssel 0, bei Umsatzsteuer der Satz in Spalte 40); der Export zählt solche Zeilen (`export_to_datev(rates_without_key=…)`, `DatevStack.hint`), und
  unter dem DATEV-Formular steht ein fester Hinweis, sobald die Stammdaten so einen Satz führen (`DATEV_RATE_NOTE`) – der Stapel bleibt Byte für Byte gleich.
  **Bekannte Grenzen:** kein Wartekonto für weitere Sätze (ein Beleg mit 5,5 % bucht für keinen Satz eine Forderung); ein Eintrag mit genau 100 % lässt sich speichern, der Beleg aber nicht
  versenden; Reverse Charge, innergemeinschaftliche Lieferung und Ausfuhr gibt es nicht (`docs/aufgaben.md`). Keine Steuerberatung in den Texten.
  **Seit 2.8.2:** Ein Eintrag, den der bisherige Payload trägt (`legacy_expressible`: 19/7 % ohne Erlöskonto), schreibt auch über `taxRateId`
  keine Momentaufnahme (`handlers._snapshot_to_write`) – „öffnen und unverändert speichern“ ändert so keine Spalte; die Prüfungen der ID bleiben,
  `TAX_SAME_RATE_TWICE` gilt nach dem, was gespeichert wird. Das Erlöskonto jeder zu schreibenden Momentaufnahme wird als Erstes in der
  Schreibtransaktion geprüft (`_refuse_gone_revenue_accounts`, `Database.missing_revenue_coas`): fehlt es inzwischen, `TAX_CHOICE_CHANGED`, nichts
  gespeichert, keine Nummer verbraucht. **Die Masken ersetzen nichts über die fertige Seite:** Platzhalter (`__…__`) nur in konstanten
  Skriptblöcken ersetzen, nie per `s.replace` über eine Seite, in der schon Werte aus der Datenbank stehen – ein Befreiungsgrund oder Kundenname,
  der wie ein Platzhalter aussieht, bräche Attribut und JSON (`tests/test_mask_placeholder_values.py` prüft es statisch und mit feindlichen Texten).
  `tests/test_tax_entries_pure.py`, `test_tax_rates_schema_2008.py`, `test_tax_rates_db.py`, `test_tax_rates_page.py`, `test_tax_entries_save.py`, `test_tax_entries_storno.py`,
  `test_tax_entries_bookings.py`, `test_tax_entries_reports.py`, `test_tax_entries_documents.py`, `test_tax_entries_demo_samples.py`, `test_tax_accounts_reports.py`,
  `test_standard_accounts_purchase.py`
- **Schriften:** Liberation Sans (2.x, OFL 1.1) aus `export/fonts/` – metrisch
  = Helvetica, daher Layout unverändert; als Untermenge per `fontTools`
  eingebettet, Content-Streams bleiben WinAnsi. sRGB-ICC für den OutputIntent
  erzeugt `tools/make_srgb_icc.py` (Normwerte, keine Fremdlizenz)
- **Validierung:** CI-Job `einvoice` (`tests.yml`) erzeugt per
  `tools/make_einvoice_samples.py` Beispiele aus den Demodaten und prüft sie mit
  Mustang (feste Version, SHA-256 gepinnt; XSD/Schematron/veraPDF); die Dateien
  hängen als Artefakt am Lauf. Mustang-Version nur mit neuer Prüfsumme ändern
- **Leistungsdatum ist Pflicht** (§ 14 UStG): Formular `required`, vorbelegt mit dem Rechnungsdatum,
  Server lehnt Speichern ohne ab (`handle_invoice_save`; nicht bei Angeboten, nicht bei bereits
  versendeten Rechnungen/Importen); steht im PDF-Kopfblock und in CII/UBL. Angebot → Rechnung setzt es auf das Rechnungsdatum
- **Kopfblock im PDF wie im Eingabeformular (seit 2.5.2):** Reihenfolge und Bezeichnungen oben rechts folgen der Maske –
  Rechnung: Rechnungsdatum, Leistungsdatum, Rechnungs-Nr., Kunden-Nr. (dann „Fassung“ bei einer berichtigten); Angebot: Datum,
  gültig bis, Angebots-Nr., Kunden-Nr. (in beiden die Daten zusammen, dann die Nummern). Was fehlt, bekommt keine Zeile. Ändert sich die Maske, zieht das PDF mit
  (`tests/test_pdf_header_block.py`; die Beschriftungsspalte `META_LABEL_X`…`META_VALUE_X` fasst die längste Bezeichnung).
  Abgelegte Dokumente bleiben, wie sie sind
- **Hinweis auf § 19 UStG (seit 2.3.3):** Ein Beleg ohne Steuerausweis (`Invoices.TaxRate` = Platzhalter −1, Häkchen
  „Umsatzsteuer ausweisen“ aus) trägt den Hinweis auf die Steuerbefreiung für Kleinunternehmer automatisch – Rechnung,
  Stornorechnung und Angebot. EIN Wortlaut an EINER Stelle (`export/einvoice_model.EXEMPT_REASON_KLEINUNTERNEHMER`) für PDF
  (Zeile unter dem Summenblock, `draw_item_table(…, tax_note=…)`), CII (BT-120) und UBL. Ob das PDF ihn druckt, entscheidet
  `small_business_note`: nur beim Platzhalter (nicht bei 0 %), und nur dann nicht, wenn ein gedruckter Freitext des Belegs –
  Rechnung: Zahlungshinweis; Angebot: Einleitung, Schluss – das Gesetz ausdrücklich zitiert („§ 19“/„§ 19a“, höchstens
  „Abs./Satz n“ dazwischen, direkt gefolgt von „UStG“/„Umsatzsteuergesetz“). Geprüft wird der Text, wie das PDF ihn druckt
  (Richtext erst durch `sanitize_richtext`); Positionstexte, das Wort „Kleinunternehmer“ allein oder „§ 19 BGB“ unterdrücken
  nichts. Ein fehlender Hinweis wiegt schwerer als ein doppelter: die Erkennung nicht lockern. Abgelegte Dokumente bleiben,
  wie sie sind. **Das Häkchen eines neuen Belegs** (ohne Quelle: kein Bearbeiten, Kopieren, Umwandeln, Neu-Ausstellen) folgt
  der jüngsten gewöhnlichen Rechnung (`Database.last_document_shows_tax`: keine Stornorechnung, keine Ersatzrechnung, kein
  Angebot; ohne eine solche ist es an); startet es abgehakt, sagt die Maske das sichtbar
- **Pflichtangaben (seit 2.3.5, `export/einvoice_model.py`):** **Steuernummer und USt-IdNr.** der eigenen Firma werden an EINER Stelle
  unterschieden (`tax_id_kind`: eine vorangestellte Bezeichnung wie „USt-IdNr.“ oder „St.-Nr.“ fällt weg; USt-IdNr. ist nur, was –
  ohne Leerzeichen/Bindestriche/Punkte – mit einem bekannten Länderpräfix beginnt und dessen Format hat, z. B. DE + 9 Ziffern;
  alles andere ist eine Steuernummer, auch „HRB 12345“ oder ein zu kurzes „DE12345678“) – für die Momentaufnahme der Rechnung wie für den Rückgriff auf den Kontakt, in CII (`VA`/`FC`), UBL (`VAT`/`TAX`) und im
  PDF-Fuß („USt-IdNr.:“ bzw. „Steuer-Nr.:“; der Fuß liest die Momentaufnahme, den Kontakt nur, wenn sie leer ist – Rechnung und Angebot).
  Hat der Verkäufer nur eine Steuernummer, steht sie zusätzlich als Verkäuferkennung (BT-29, `Party.identifier`) in CII und UBL –
  ohne sie lehnt der Validator die E-Rechnung ab (BR-CO-26); mit USt-IdNr. gibt es kein BT-29.
  Eine Käuferangabe, die keine USt-IdNr. ist, geht nicht in die E-Rechnung. **Skonto** (`Invoices.SkontoDays`/`SkontoPercent`,
  `parse_skonto`: Tage 1–365, Prozent zwischen 0 und 100 mit höchstens zwei Nachkommastellen – geprüft am Text bzw. an der Zahl,
  BEVOR etwas umgerechnet wird (ein Wert wie `1e9999999` darf den Server nicht beschäftigen); beim Speichern einer nicht eingefrorenen
  Rechnung geprüft, nur bei positivem Brutto und gültigem Rechnungsdatum, die Frist darf nicht nach der Fälligkeit enden) steht im PDF als Absatz nach dem Zahlungshinweis (Frist ab
  Rechnungsdatum, Abzug vom gedruckten Brutto in EINEM Schritt halb auf den Cent gerundet – `skonto_amount`) und in der E-Rechnung als
  erste Zeile der Zahlungsbedingungen in der Schreibweise der XRechnung (`#SKONTO#TAGE=7#PROZENT=2.00#` + Zeilenumbruch, dann der
  Freitext); die Stornorechnung trägt nie Skonto; gebucht wird Skonto bei der Zahlungszuordnung (seit 2.9.0, *Skonto bei der Zahlungszuordnung*). **Lückenprüfung**
  (`einvoice_gaps`): Fehlen Angaben, die die XRechnung verlangt (Leitweg-ID/Käuferreferenz, IBAN, PLZ/Ort, Kontaktname, Telefon und
  E-Mail der eigenen Firma, E-Mail des Kunden, ein Zahlungshinweis ohne „#“ – das Zeichen gehört in der XRechnung der Skonto-Angabe …), wird die UBL-Datei **nicht geschrieben** (ein abgelegtes XML eines Entwurfs wird
  entfernt – am gespeicherten Pfad und am Namensschema, nur Dateien mit der Endung `.xml`, nie das PDF), das PDF mit CII entsteht immer; das Ergebnis trägt den Klartext („XRechnung (XML) nicht erstellt – es fehlt: …“), die
  Rechnungsseite zeigt die Lücken bei Entwurf/abgeschlossen, und vor dem Versenden fragt sie nach. Die UBL-Datei entsteht nur bei
  Leitweg-ID/Käuferreferenz (`wants_xrechnung`; eine Bestellnummer allein löst sie nicht aus). Der Kontoname (BT-85) wird nicht
  geschrieben (den Kontoinhaber gibt es nicht als Momentaufnahme). `tools/make_einvoice_samples.py` bricht ab, wenn ein Beispiel Lücken
  hat – Prüfung und Mustang müssen übereinstimmen
- Versendete und stornierte Rechnungen (`db.invoice_lock.FROZEN_STATUSES`) werden von
  „PDF + E-Rechnung“ nicht neu erzeugt (`overwrite_sent` nur beim Eintritt aus Entwurf/abgeschlossen in
  „versendet“); liegt für sie noch keine Datei vor (Import, gescheiterte Erzeugung), darf sie erzeugt werden
- **Erzeugt** wird nur an zwei Stellen: über „PDF + E-Rechnung“ im Formular und
  automatisch beim Wechsel von Entwurf/abgeschlossen auf Status **versendet** (ab da ist die Rechnung
  eingefroren). Eine bereits versendete Rechnung wird nicht überschrieben; versendet ↔ überfällig erzeugt nichts
- **Sperre versendeter Rechnungen** (`db/invoice_lock.py`, seit 2.1.2). Eine Stelle entscheidet
  (`check_changes`, Statusgruppen), und jeder Schreibweg auf eine Rechnung geht darüber: das Speichern
  (`Database.update_invoice_fields`, bei einer versendeten Rechnung nur aus `correct_invoice`) und der
  Statuswechsel (`Database.set_invoice_status`) – nie
  `update_invoice_status` (der ungeprüfte Baustein für Seed und Tests; Angebote: `set_quote_status`). Bearbeitbar ist alles bei
  `draft`/`finalized`. Bei `sent`/`overdue`/`partial_payment`/`partial`/`paid` sind Nummer, Rechnungsdatum,
  Kunde, eigene Firma, Positionen, Steuer, Summen, Fälligkeit, Skonto, Bank, Währung und Käufer-USt-IdNr.
  gesperrt: `/invoice/save` antwortet mit **409** und einem Klartext, nichts der Anfrage wird geschrieben.
  Änderbar bleiben der Zahlungshinweis (`PaymentTerms`, der Text unter den Positionen), die Anschrift,
  Bestellnummer, Leitweg-ID und Leistungsdatum – aber nur als **Berichtigung** (siehe dort). `cancelled` und
  die Stornorechnung (`row_group`: `InvoiceKind='storno'` gilt als abgeschlossen, gleich welchen Status sie trägt):
  gar nichts mehr. Geprüft wird gegen den gespeicherten Stand **in der Transaktion** (`BEGIN IMMEDIATE`, das
  `UPDATE` trägt die Statusgruppe im `WHERE`): zwei gleichzeitige Anfragen laufen nacheinander. Was das
  Formular unverändert zurückschickt, ist kein Verstoß (Beträge in Minor Units, Positionen Feld für Feld,
  Einheiten wie `unit_display` sie zeigt, Steuersatz 0,19 = 19). **Die Maske überschreibt bei einer
  versendeten Rechnung nichts still:** Sie zeigt die gespeicherten Werte (Anschrift der Rechnung, Leitweg-ID)
  und holt beim Laden nichts aus den Stammdaten; sie schickt nur die Felder, die der Nutzer geändert hat (kein
  `items`), und der Server schreibt nur, was vom Gespeicherten abweicht (`changed_open_fields`) – ein
  unverändertes Speichern ändert keine Spalte, auch nicht `NULL` zu leer (und legt weder Fassung noch
  Protokoll an). Die Anschrift ist eine
  Momentaufnahme und kommt nur auf ausdrücklichen Wunsch (`refreshAddress`, Kästchen „Anschrift aus den
  Stammdaten übernehmen“, mit alt → neu, nur bei Abweichung) neu aus den Stammdaten **desselben** Kunden; Absender,
  Bank und Käufer-USt-IdNr. sind abgeleitet und werden bei Sperre nie neu geholt. **Status und `AmountDue` kommen nie vom Client:** eine
  neue Rechnung ist `draft` (oder `finalized`), `AmountDue` ist im Entwurf das Brutto, danach rechnet es die
  Zahlungslogik. Ein unbekannter Status gilt als gesperrt
- **Berichtigen** (`Database.correct_invoice`, `db/invoice_versions.py`, seit 2.2.0): Das Speichern der änderbaren
  Felder einer versendeten Rechnung ist eine Berichtigung – kein eigener Schreibweg, es bleibt bei `/invoice/save`
  mit dem Teil-Payload (Knopf „Berichtigen“, nach Rückfrage). Gesperrte Felder werden zuerst abgelehnt; ohne Abweichung
  vom Gespeicherten passiert nichts – auch ohne `documentVersion`. **Das Leistungsdatum bleibt ein Datum:** Steht es
  unter den geänderten Angaben, muss es ein ISO-Datum sein (`CorrectionInvalid`, 400, vor dem Verschieben der
  Dokumente; die Maske schickt ein geleertes gar nicht erst ab) – ein unverändertes, auch leeres (Import), ist kein
  Verstoß.
  Sonst in EINER Transaktion (`BEGIN IMMEDIATE`, die Rechnung wird neu gelesen): Die Fassung, die die Maske
  geladen hat (`documentVersion`; `Invoices.DocumentVersion`, NULL = 1), muss die aktuelle sein, sonst 409 (fehlt
  sie oder ist sie veraltet, wird nichts geschrieben); PDF und XML wandern nach `Fassungen/<ID>/`
  (`archive_sent_documents`) und stehen mit Fassung, Pfad (relativ zu `invoices/`) und SHA-256 in
  `InvoiceDocumentVersions`, ihre Pfade in der Rechnung werden geleert; jedes geänderte Feld ist eine Zeile in
  `InvoiceCorrections` (`Action='correct'`: wer, wann, alt, neu, neue Fassung – bei `refreshAddress` je
  Anschriftsspalte eine); `DocumentVersion` zählt um eins hoch. Scheitert das Verschieben, das Festhalten in
  `InvoiceDocumentVersions` (`_archive_documents`) oder der Commit, liegen die Dateien wieder am alten Platz (409,
  nichts geändert). Danach erzeugt `handlers._correct_invoice` das Dokument
  der neuen Fassung – nur, wenn eines abgelegt war (ein Import bekommt Felder und Protokoll, kein Dokument).
  Scheitert das Erzeugen, bleibt die Berichtigung gespeichert, die Antwort trägt eine Warnung, und „PDF +
  E-Rechnung“ erzeugt die fehlende Datei (die Regel „eingefroren, aber keine Datei vorhanden“). **Die berichtigte
  Fassung trägt einen Hinweis** (PDF: Zeile vor der Positionstabelle und „Fassung: n“ im Kopf; CII BT-22; UBL
  `cbc:Note`; TypeCode 380 und Nummer bleiben): Er ergibt sich aus dem Stand der Datenbank
  (`correction_notice`: `DocumentVersion` > 1 und Berichtigungen zur aktuellen Fassung, keine Rückkehr auf
  Entwurf danach), nicht aus einem Parameter – auch ein später über „PDF + E-Rechnung“ erzeugtes Dokument trägt
  ihn, eine nach dem Zurückholen neu versendete Rechnung nicht. „Ersetzt die Fassung vom …“ nennt den jüngsten
  Protokolleintrag vor der ersten Berichtigung der aktuellen Fassung, der eine Berichtigung oder ein Versand
  (Entwurf/abgeschlossen → versendet) ist, sonst `SentAt`, sonst entfällt der Teil. Zeitpunkte stehen in UTC,
  Dokumente und Oberfläche nennen die Ortszeit. Die stornierte Rechnung bleibt ganz gesperrt. Eine Ersatzrechnung
  (*Stornobeleg*) trägt daneben ihren eigenen Hinweis „Ersetzt die stornierte Rechnung …“: Trifft beides zu, stehen beide
  Hinweise, der der Ersetzung zuerst
- **Statusübergänge** (`db.invoice_lock.TRANSITIONS`; die Statusauswahl bietet nur die erlaubten Ziele,
  `set_invoice_status` lehnt den Rest mit Klartext ab – 409, „bezahlt“ ohne Zahlung 400): Entwurf ↔
  abgeschlossen frei. **Entwurf/abgeschlossen → versendet** („Versenden“) ist der einzige Übergang, der die
  Forderung bucht und das Dokument erzeugt. Versendet ↔ überfällig bucht und erzeugt nichts (auch bei einem
  Import ohne Forderung der App). Teilzahlung/bezahlt und der Rückweg setzt die Zahlungslogik
  (`next_payment_status`); „bezahlt“ von Hand nur mit mindestens einer Zahlung (Skonto), der Rückweg von
  bezahlt/Teilzahlung nach versendet/überfällig von Hand nur ohne Zahlung (verwaister Zustand).
  Versendet/überfällig → Entwurf/abgeschlossen („zurückholen“) nur ohne Zahlung; die Forderung wird entfernt
  und die versendeten Dokumente wandern nach „Fassungen“ (siehe dort).
  **Nach „storniert“ geht der Status nur für eine Rechnung ohne positiven Betrag** (Brutto ≤ 0: selbst angelegte Gutschrift,
  0-€-Rechnung – sie bekommt keine Stornorechnung), mit dem bisherigen Buchungsverhalten (Art `cancel`: die Forderung geht,
  solange keine Zahlung daran hängt und nur die der App). Das entscheidet `check_transition` am Betrag, den
  `set_invoice_status` in der Transaktion liest (Bedingung `NO_POSITIVE` in `TRANSITIONS`; `check_transition` und
  `allowed_targets` nehmen ihn als `gross`, ohne Angabe gilt er als positiv). Für Brutto > 0 lehnt
  `set_invoice_status(…, 'cancelled')` mit 409 ab: Storniert wird über „Stornieren“ (Absatz *Stornobeleg*); die Seite zeigt den
  Knopf nur bei Brutto > 0 und „Storniert“ in der Statusauswahl nur bei Brutto ≤ 0. Storniert → versendet/
  überfällig („wiederbeleben“) gibt es nur für eine stornierte Rechnung **ohne** Stornorechnung (Altbestand,
  Import, von Hand storniert): bucht die Forderung neu und lässt ein vorhandenes Dokument stehen; mit Stornorechnung 409 – sie bleibt
  storniert. Die Stornorechnung selbst ändert ihren Status nie (409).
  **Status, Forderungsbuchung und (beim Zurückholen) das Ablegen der Dokumente laufen in EINER Transaktion**
  (`set_invoice_status`, `BEGIN IMMEDIATE` von der Prüfung bis zum Commit): Sie gelingen gemeinsam oder gar nicht;
  nur das Erzeugen des Dokuments beim Versenden folgt danach (`handle_update_invoice_status`). **Jeder gelungene
  Wechsel steht im Protokoll** (`InvoiceCorrections`, `Action='status'`, in derselben Transaktion; die
  Zahlungslogik `_recalc_invoice` protokolliert nicht), und `send` setzt `SentAt` beim ersten Versand (Importe
  lassen es NULL). Konten, die
  das Buchen anlegen könnte, werden vorher in eigenen Verbindungen aufgelöst (`_receivable_accounts`), nie
  innerhalb. **Die App entfernt nur die Forderung, die sie selbst gebucht hat** (`_receivable_removal`: ohne
  `SourceGroup`, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet) – beim Zurückholen, beim Storno von Hand
  (Brutto ≤ 0) und beim Entfernen der letzten Zahlung einer von Hand stornierten Rechnung (`_drop_receivable`): nie ein
  Satz mit Quellklammer. Hängt an der Rechnung eine importierte, von Hand gebuchte oder an eine Bankbewegung gehängte
  Forderung oder ein Zahlungssatz, lehnt das Zurückholen mit Klartext ab (409, nichts geändert); Stornieren, Storno von
  Hand und Zahlungsentfernen lassen sie stehen. **Beim Versenden einer Ersatzrechnung** (`ReplacesInvoiceId`, Art `send`)
  **ziehen die Zahlungen der stornierten Rechnung auf sie um** – in derselben Transaktion, nach der Forderungsbuchung
  (`_payment_move_context` löst das Erlöskonto vorher in eigenen Verbindungen auf, `_move_replaced_payments` schreibt;
  `update_invoice_status`, der ungeprüfte Baustein für Seed und Tests, zieht nie um). Voraussetzung: gleicher Kunde und
  gleiche Währung und ein hinterlegtes Erlöskonto für den Steuersatz der neuen Rechnung – sonst bleibt alles, und der
  Hinweis sagt es. **Trägt die stornierte oder die neue Rechnung einen Steuersatz je Position, zieht nichts um** (`MOVE_MIXED_HINT`, nach einer Erstattung mit dem Satz dazu,
  *Mehrere Steuersätze je Beleg*). **Nach einer Erstattung zieht höchstens das verbleibende Guthaben um:** `movable_left` = alle Zuordnungen der
  stornierten Rechnung (auch eine negative des WISO-Imports: Sie zieht nie um, mindert aber das Guthaben) minus die
  Erstattungen an der Stornorechnung, vor dem Umzug; je umziehbarer Zuordnung ist der Teil `min(Betrag, Rest der neuen
  Rechnung, movable_left)`, bei `<= 0` endet der Umzug, nach einem gelungenen Umzug sinkt `movable_left` um den Teil, der Rest
  der Zuordnung bleibt wie sonst an der stornierten Rechnung (`allow_cancelled`). So ist danach nie mehr erstattet als gezahlt
  (50,00 € gezahlt, 20,00 € erstattet, Ersatzrechnung über 119,00 €: 30,00 € ziehen um, 20,00 € bleiben, Guthaben 0); ohne
  Erstattung bremst `movable_left` nie. Der Hinweis nennt das Erstattete („… sind bereits erstattet und bleiben an der
  stornierten Rechnung.“); „bleiben als Guthaben des Kunden“ nennt nur das verbleibende Guthaben, ohne den erstatteten Teil.
  Umgezogen wird je Zuordnung (älteste zuerst, solange die neue Rechnung offen ist), und nur, was die
  App **genau so geschrieben hat** und sich eindeutig neu schreiben lässt (`_movable_payment`: Bankbewegung mit
  Zahlungseingang und SKR-Konto, nicht überbucht, ohne Sätze mit `SourceGroup`, genau eine Zuordnung zum Paar).
  **Die Sätze der Zuordnung müssen genau die sein, die `_assign_in_tx` dafür schreibt** (`_revenue_coa_if_as_written`,
  Vergleich mit `payment_rows` – derselben Stelle, aus der das Schreiben seine Werte nimmt): der Bestand („Bank an
  Debitoren“ und „Wartekonto an Erlöskonto“ oder der Einzelsatz „Erlöskonto an Bank“, keiner mehr, keiner weniger), das
  Bankkonto der Bewegung, die übrigen Konten in ihrer Rolle, Betrag, Datum, Steuerdatum, Empfänger, Währung, Kunde, Text,
  Steuersatz, der Steuerbetrag (anteilig – oder der Rest, aber nur, wenn die Rechnung voll bezahlt war: Nur der schließende
  Teil bekommt ihn; `_prorated_tax`, `_remaining_tax`), Debitoren- und Wartekonto die der Forderung (`_receivable_sides`,
  dieselbe Stelle, aus der `_assign_in_tx` sie nimmt), keine Kategorie; kein Beleg und kein anderer Verweis an den Sätzen, den `_delete_booking_rows` wegräumt
  (`_booking_attachments_exist`, `BOOKING_REFERENCES`), unter der Bewegung kein markierter Satz mit einer Beleg-Nr.,
  die zu keiner ihrer Zuordnungen gehört, und kein Satz ohne Marke, der die Beleg-Nr. der Rechnung trägt oder die Debitoren
  berührt (ein von Hand ergänztes Skonto „… an 10000“ bliebe sonst zurück, und die neue Rechnung zeigte einen offenen Rest).
  Bei gleichem Steuersatz muss außerdem das Erlöskonto der Sätze das heute hinterlegte sein – der Umzug bucht auf dieses und
  verschöbe den Erlös sonst im vergangenen Zeitraum auf ein anderes Konto. Sonst bleibt die Zuordnung liegen – der Umzug löschte die Sätze und schriebe sie
  neu, eine von Hand korrigierte Steuer, ein geändertes Datum oder ein verknüpfter Beleg gingen verloren, ein markierter
  Satz mit geänderter Beleg-Nr. bliebe als Doppelbuchung stehen. Beurteilt wird jede Zuordnung gegen den Stand VOR dem
  ersten Umzug (der Steuer-Rest des schließenden Teils hängt von den anderen ab). Importe (Zuordnung ohne Sätze),
  überbuchte Bewegungen und geänderte Sätze bleiben an der stornierten Rechnung; eine Sammelzahlung zieht nur mit dem Anteil
  der stornierten Rechnung um, die Zuordnungen anderer Rechnungen bleiben. Je Zuordnung wird sie samt Sätzen entfernt
  (`_unassign`), die neue Rechnung
  bekommt den Betrag, den sie noch braucht (`_assign_in_tx`: Datum der Bankbewegung, Steuersatz und Erlöskonto der neuen
  Rechnung; Steuer anteilig, der schließende Teil den Rest), und ein Rest wird an der stornierten Rechnung wieder
  zugeordnet (`allow_cancelled`, auf dem bisherigen Erlöskonto): Er ist ihr Guthaben. Weicht der Steuersatz ab, ändert sich
  die Umsatzsteuer am Zahlungstag; bei gleichem Satz und Betrag bleibt die EÜR unverändert. **Je Zuordnung läuft das in
  einem Savepoint:** Lässt sie sich nicht vollständig mit den erwarteten Beträgen neu buchen, geht er zurück (Zuordnung und
  Sätze unverändert, ihre Einträge im Replay-Log entfallen), sie zählt als übersprungen, der Versand gelingt, die nächste
  Zuordnung kommt an die Reihe; nur ein unerwarteter Datenbankfehler lehnt den Versand ab (409, `PaymentMoveFailed` →
  `_Refusal`) und nichts ist geändert. **Was geschah, nennt `hint` in der Antwort** (`StatusChange.payment_hint`) – und
  bei dem, was bleibt, den wirklichen Grund (`MOVE_SKIP_REASONS`: Import, Bankkonto ohne SKR-Nummer, überbuchte Bewegung,
  Sätze ohne Marke (ältere Version, von Hand angelegt), geänderte oder ergänzte Sätze oder Beleg, ein anderes Erlöskonto
  als das hinterlegte, allein ein anderes Steuerdatum – so stempelt der DATEV-Export die exportierten Sätze, sie
  ziehen deshalb nicht um –, mehrfach zugeordnete Bewegung, keine Bankbewegung mit Zahlungseingang, nicht neu buchbar), je
  Grund einmal in fester Reihenfolge, mit dem Betrag des Übersprungenen insgesamt. Entfernt der Nutzer die Zahlung an der
  neuen Rechnung später von Hand, wandert nichts zurück: Der Betrag ist freier Rest der Bewegung (Guthaben in den offenen
  Posten) und lässt sich jeder offenen Rechnung zuordnen
- **Stornobeleg** (`Database.cancel_invoice`, `db/invoice_storno.py`, seit 2.2.1; `POST /invoice/cancel`, JSON
  `{"invoiceId", "date"}` – `invoiceId` streng als Ganzzahl wie bei `/invoice/save` –, Route in `_JSON_PATHS` und
  `_POST_ONLY_JSON_PATHS`: CSRF-Token und Herkunft wie `/invoice/status`, GET → 405): Eine versendete Rechnung
  (versendet/überfällig/Teilzahlung/bezahlt) wird über den Knopf „Stornieren …“ storniert, mit Rückfrage und Stornodatum
  (vorbelegt mit heute, nicht vor dem Rechnungsdatum und nicht nach heute; `appConfirm` hat dafür ein optionales Datumsfeld
  mit `min`/`max`) – der Knopf erscheint nur bei Brutto > 0, eine Rechnung ohne positiven Betrag wird über den Status
  storniert (*Statusübergänge*). Es entsteht ein
  eigener Beleg, die **Stornorechnung**: Nummer aus dem Rechnungskreis des **Jahres des Stornodatums**
  (`allocate_invoice_number`), Kopf der Ursprungsrechnung (Anschrift-Momentaufnahme, Leistungsdatum, Bank, Steuer, Leitweg-ID …)
  ohne Fälligkeit und Skonto, `PaymentTerms` = „Der Betrag wird verrechnet bzw. erstattet.“ (`STORNO_PAYMENT_TEXT`),
  `InvoiceKind='storno'`, `CancelsInvoiceId`, Status `sent` mit `SentAt`, **Summen exakt negiert** (Minor Units mal −1,
  nichts neu gerechnet), Positionen mit negativer Menge und positivem Einzelpreis, `AmountDue` = ihr (negatives) Brutto.
  Die Ursprungsrechnung wird `cancelled`. Alles in EINER Transaktion (`BEGIN IMMEDIATE`: Prüfung, Nummer, Beleg, Positionen,
  Status, je eine Protokollzeile für beide Belege, Gegenbuchung, Neuberechnung) – ganz oder gar nicht, nach einem Fehler ist
  keine Nummer verbraucht; die Konten der Forderung löst sie vorher in eigenen Verbindungen auf, aber erst, wenn die Rechnung
  sich stornieren lässt. Abgelehnt wird mit Klartext und Statuscode, nichts geschrieben: Stornodatum kein ISO-Datum, vor
  dem Rechnungsdatum oder **in der Zukunft** (400; maßgeblich ist `_today()`, das Ortsdatum des Servers – sonst legte ein
  Tippfehler wie 2062 einen Nummernkreis 2062 an; nur die Demodaten übergeben ihr Szenariodatum als `today`), unbekannte ID oder
  Angebot (404), Stornorechnung, Entwurf/abgeschlossen, schon mit Stornorechnung storniert, unbekannter Status, Brutto ≤ 0,
  schon eine Stornorechnung (409; der eindeutige Index auf `CancelsInvoiceId` fängt es zusätzlich ab); scheitert das Speichern an einem
  Verweis ins Leere (`ForeignKeyError`, etwa ein gelöschtes Bankkonto), nennt die Meldung ihn im Klartext (409, nichts
  „repariert“). **Die Forderung wird nie gelöscht:** Hat die App sie gebucht (`_is_app_receivable`, dieselbe Einordnung
  wie in `_receivable_removal`, nur dass es nicht zählt, ob sie an einer Bankbewegung hängt), bucht die Stornierung die
  **Gegenbuchung** („Debitoren an Wartekonto“ über `_book_receivable`, Form `transfer`, negativer Betrag und negative Steuer,
  Datum und Beleg-Nr. der Stornorechnung – für `receivable_condition` die Forderung einer Gutschrift); sonst (Import,
  Steuersatz ohne Wartekonto, importierte oder von Hand gebuchte Forderung) keine, und die Antwort trägt einen `hint`. **Ein Beleg mit
  Steuersatz je Position** bucht die Gegenbuchung je Satzgruppe, die Stornorechnung trägt Kennzeichen und negierte Positionen
  (*Mehrere Steuersätze je Beleg*). Zahlungen
  bleiben an der stornierten Rechnung (der Umzug auf die Ersatzrechnung: *Statusübergänge*; die Erstattung: unten).
  Ihr Betrag ist das **Guthaben des Kunden: Σ `InvoicePayments.Amount` über beide Belege des Paares** (Rechnung und
  Stornorechnung; `get_storno_credits`, `get_storno_credit`, `get_storno_credits_for_contact` – rein lesend, ein Statuswechsel
  der Stornorechnung gehört nicht dazu). Es steht in der Rechnungsliste an der Stornorechnung („Erstattung offen: …“ statt
  „Stornorechnung“; nach voller Erstattung wieder „Stornorechnung“), als Zeile auf den Seiten beider Belege (ganz erstattet:
  „Guthaben vollständig erstattet.“) und als Abschnitt „Guthaben aus stornierten Rechnungen“ bei den
  offenen Posten des Kontakts (`storno_credits`; der Saldo zieht es ab, denn 10000 führt es im Haben). Alle zeigen das
  verbleibende Guthaben.
  **Erstattung** (`Database.assign_storno_refund`, `db/invoice_payments.py`, seit 2.2.3; Weg: `POST /invoice/link-payment` mit
  der ID der Stornorechnung, `handle_link_invoice_payment` erkennt sie – keine neue Route, Prüfungen unverändert): Eine
  ausgehende Bankbewegung (Betrag < 0, Bankkonto mit SKR-Nummer) gleicht das Guthaben aus, zugeordnet **an der Stornorechnung**
  mit negativem Betrag; erstattet wird min(Guthaben, freier Rest der Bewegung, gewünschter Betrag auf Cent gerundet). Die Sätze
  schreibt `payment_rows` (wie bei der Zahlung) mit Datum, Steuerdatum, Empfänger und Währung der Bewegung, der **Nummer der
  Stornorechnung** als Beleg-Nr. und der Marke `invoice_payment`, alle negativ: Hat die Stornorechnung eine Gegenbuchung
  (`_receivable_sides`), „Bank an Debitoren“ und „Wartekonto an Erlöskonto“, sonst der Einzelsatz „Erlöskonto an Bank“.
  **Bei einem Beleg mit Steuersatz je Position** (Etappe 2b) erstattet sie je Satz: Betrag, Steuer und Erlöskonto je Satz stehen in *Mehrere Steuersätze je Beleg* –
  die folgenden Regeln zu Steuer und Erlöskonto gelten für den Beleg mit EINEM Satz.
  **Steuer:** Sie nimmt zurück, was die Zahlungen an der stornierten Rechnung gebucht haben (`_allocations_tax`); gleicht die
  Erstattung das Guthaben ganz aus, ist es der Rest (Steuer der Zahlungen minus die früherer Erstattungen), sonst anteilig (Cent,
  halb auf, nie mehr als der Rest) – die Summe aller Erstattungen ist die Steuer der Zahlungen, **solange sich die Zahlungen der
  stornierten Rechnung nach der ersten Erstattung nicht ändern** (wird danach eine Zahlung entfernt oder zieht ein Teil auf die
  Ersatzrechnung um, kann die Summe um Cent abweichen). **Erlöskonto:** das der Zahlungen (`_payment_revenue_coa` über die
  markierten Sätze ihrer Zuordnungen), wenn alle dasselbe tragen; tragen sie **mehr als ein** Erlöskonto (ein Umbuchungssatz
  wurde im Editor geändert, `refund_revenue_coas_differ`), wählt der Nutzer das der Erstattung (`revenue_coa_id`, ein
  Erlöskonto, sonst Ablehnung „Die Zahlungen dieser Rechnung liegen auf verschiedenen Erlöskonten – bitte das Erlöskonto für die
  Erstattung wählen.“; kein hinterlegtes Konto entscheidet das, und es legt keines an). Ohne markierte Sätze (Import) das für den
  Steuersatz hinterlegte, sonst wählt der Nutzer. Der Kasten „Erstattung“ zeigt die Auswahl in beiden Fällen (mit dem Satz oben
  als Hinweis); „Die Wahl gilt künftig als Vorgabe für diesen Steuersatz.“ steht dort nicht – Erstattungssätze lehren kein
  Erlöskonto, bei der Zahlung bleibt der Satz. Vorprüfung vor der Transaktion (eine abgelehnte Erstattung legt kein Konto an), Prüfung
  frisch gelesen in ihr; danach `_recalc_invoice` (`AmountDue` = Brutto − Zuordnungen, der Status der Stornorechnung ändert sich
  nie). Die Bankzeile bekommt nur den Kunden nachgetragen (die Route, nicht die DB-Schicht) – sonst füllte ihn die Bankmaske beim
  ersten Speichern ein. **Entfernen** wie jede Zuordnung: Forderung und Gegenbuchung bleiben, das Guthaben ist wieder da. **Das
  Guthaben wird auf keinem Weg der Oberfläche negativ:** Jeder Weg, der eine **Zahlung** an einer stornierten Rechnung mit
  Stornorechnung entfernt, fragt vor dem ersten Schreiben in seiner Transaktion dieselbe Prüfung (`_refund_removal_blocker`:
  bliebe von den Zuordnungen beider Belege weniger als 0, lehnt `PaymentRemovalRefused` ab, nichts ist geändert; **bei einem Paar mit Steuersatz je Position gilt
  dazu die Prüfung je Satz** – wäre danach für einen Satz mehr Betrag oder Steuer erstattet, als die Zahlungen trugen, oder ein Satz ganz erstattet, aber nicht mit
  seiner ganzen Steuer, oder hätte ein Satz ohne Zahlung erstattet, lehnt `MIXED_REMOVAL_REFUSED` ab, mit Satz und Nummer der Stornorechnung („… für 7 % wäre danach
  mehr erstattet als gezahlt … Erst die Erstattung an der Stornorechnung 26R001 entfernen.“); das ist genau der Stand, in dem `split_payment` die nächste Erstattung
  ablehnte (`tax_groups.booked_fault`, am Topf der Zahlungen `refund_pool`), mit denselben Bausteinen gelesen wie die Erstattung (`_mixed_booked`, ohne die entfernten Zuordnungen:
  `_unbalanced_rate_after_removal`); gilt, wenn die stornierte Rechnung ODER ihre Stornorechnung das Kennzeichen trägt, und nach der Summe; lässt sich der Stand je Satz nicht
  lesen (`MixedNotBookable`), bleibt es bei der Summe – das Entfernen ist dort der Weg, den Zustand zu bereinigen; ein Paar mit einem Satz bleibt, wie es war): die
  Rechnungsseite (`unassign_invoice_payment`, 409), das ✕ an einer Zeile der Zahlung im Bank-Editor und das Löschen eines
  Satzes der Zuordnung (`remove_assignment_for_child`), `delete_transaction` – Satz wie Bankbewegung – und die Liste
  (`/transactions/delete`). `_unassign` selbst prüft nichts (der Umzug braucht es so). Der Editor prüft **alle** entfernten
  Zeilen zusammen schon beim Planen (`_plan_split_rows` → `Database.split_removal_blocker`, rein lesend, ein nur abgehängter Satz
  zählt nicht): abgelehnt wird vor dem ersten Schreiben (400 mit Klartext, nichts gespeichert). Seit 2.3.15 läuft das ganze
  Speichern des Editors in EINER Transaktion (`save_bank_with_rows`, siehe *Masken*): Geplant wird unter der Schreibsperre, die
  Einzelprüfung beim Entfernen bleibt zusätzlich – lehnt sie ab oder scheitert eine Zeile, ist nichts gespeichert; die Löschroute
  leitet mit dem Grund auf die Liste (`/transactions?err=`, `appMsg`). Das Entfernen einer **Erstattung** bleibt auf allen Wegen
  frei. Die Prüfliste meldet `refund_exceeds_credit` weiter für Eingriffe an der App vorbei (bei einem Paar mit Steuersatz je Position dazu `rate_refund_mismatch`). Nach einer Erstattung zieht höchstens das
  verbleibende Guthaben auf die Ersatzrechnung um (*Statusübergänge*). Die Seite der Stornorechnung zeigt den Kasten „Erstattung“ (ausgehende Bewegungen mit
  freiem Rest, Betrag vorbelegt; die Erstattungen mit „Entfernen“); die EÜR nimmt den Erlös am Tag der Erstattung zurück, das
  Dashboard zählt die Bankbewegung als Ausgabe.
  **Der Stornorechnung lässt sich keine Zahlung zuordnen** (`payment_blocker`, `assign_invoice_payment`,
  `link_invoice_to_transaction`; sie ist nie Kandidat, nie offener oder überfälliger Posten – ihre Zuordnung ist allein die
  Erstattung), und die Zahlungslogik ändert ihren Status nie. **Die Stornorechnung ist ganz gesperrt:** Status (409), Speichern, Berichtigen, Löschen, Kopieren – jede
  Abweichung wird mit „Stornorechnung: Sie lässt sich nicht ändern.“ abgelehnt, unverändertes Speichern ändert nichts. Ihre
  Seite ist schreibgeschützt und nennt die Ursprungsrechnung (Link); die Seite der stornierten Rechnung nennt die
  Stornorechnung. In der Rechnungsliste trägt die Stornorechnung statt des Status „Stornorechnung“, zeigt negative Beträge
  und zählt in keiner Summe der Statistik mit. **Dokument:** Typ 381 – CII mit `ram:InvoiceReferencedDocument` (BT-25/BT-26)
  und BT-22 „Storno zu Rechnung … vom …“, UBL als `CreditNote` mit `cac:BillingReference`, `cbc:CreditedQuantity` und ohne
  `cbc:DueDate` (derselbe Code wie die Rechnung, `export/xrechnung_invoice.py`); im XML stehen die Beträge **positiv** (das
  Modell negiert genau einmal), das PDF zeigt die gespeicherten negativen Beträge mit dem Titel „Stornorechnung“ und „zu
  Rechnung Nr. … vom …“; sie trägt nie den Berichtigungs-Hinweis. Das Dokument der Ursprungsrechnung bleibt unangetastet.
  Erzeugt wird es nach dem Commit (`handle_invoice_cancel`; ein Fehler dabei lässt die Stornierung stehen, die Antwort trägt
  eine `warning`, „PDF + E-Rechnung“ erzeugt die Datei nach). Der E-Mail-Versand nennt sie „Stornorechnung“ und fordert nichts
  an.
  **Neu ausstellen** (`Database.create_replacement_draft`, `db/invoice_storno.py`, seit 2.2.2; `POST /invoice/reissue`, JSON
  `{"invoiceId"}`, Route in `_JSON_PATHS` und `_POST_ONLY_JSON_PATHS` wie `/invoice/cancel`; der Knopf „Stornieren und
  neu ausstellen …“ schickt `reissue: true` an `/invoice/cancel` – ein Wert, der kein Wahrheitswert ist, ist eine
  ungültige Anfrage, bevor etwas geschieht): Zu einer stornierten Rechnung **mit** Stornorechnung legt die App den Entwurf
  einer neuen Rechnung an – Kopf und Positionen der stornierten (wie bei der Stornorechnung, aber positiv, nichts neu
  gerechnet; Leistungsdatum, Zahlungshinweis und Skonto übernommen), Nummer aus dem Kreis des Jahres von heute (`_today()`),
  heutiges Datum, Fälligkeit um dasselbe Zahlungsziel verschoben (sonst leer), Status `draft`, `Invoices.ReplacesInvoiceId`
  = die stornierte Rechnung; ein Entwurf bekommt, wie jeder, keine Protokollzeile. **Höchstens eine Ersatzrechnung je
  stornierter Rechnung** (Prüfung in der `BEGIN IMMEDIATE`-Transaktion, einen eindeutigen Index gibt es nicht; nach dem
  Löschen des Entwurfs geht es wieder, und seine Nummer ist frei, solange er nie versendet war). Abgelehnt wird mit Klartext,
  nichts geschrieben, kein Zähler verbraucht: unbekannte ID oder Angebot (404); Stornorechnung, nicht stornierte Rechnung,
  stornierte ohne Stornorechnung, schon ersetzte Rechnung, Nummernkreis ohne brauchbare Nummer, Fehler beim Speichern –
  ein Verweis ins Leere steht im Klartext (409). Bei „Stornieren und neu ausstellen“ ist der Entwurf eine zweite
  Transaktion nach Stornierung und Dokument (`handle_invoice_cancel`): Scheitert er, bleibt die Stornierung stehen, und die
  Antwort trägt eine `warning` („Neu ausstellen“ an der stornierten Rechnung holt es nach). „Als neu kopieren“
  (`copy_from`) setzt nie `ReplacesInvoiceId`. **Dokument:** Die Ersatzrechnung trägt „Ersetzt die stornierte Rechnung …
  (Stornorechnung …)“ (PDF: fette Zeile vor der Positionstabelle; CII BT-22; UBL `cbc:Note`; TypeCode 380) – aus dem Stand
  der Datenbank (`replacement_note`), nicht aus einem Parameter; ihre Seite nennt die stornierte Rechnung, die stornierte
  nennt die neue.
  **Nachträglich stornieren:** Eine von Hand stornierte Rechnung ohne Stornorechnung (Altbestand) und mit Brutto > 0 bekommt
  sie über „Stornorechnung nachträglich erstellen …“ (`cancel_invoice` auf eine Rechnung im Status `cancelled`: die
  Rechnung bleibt mit Status und Protokoll, wie sie ist; die Gegenbuchung entsteht nur, wenn ihre Forderung der App noch
  in den Büchern steht, sonst trägt die Antwort den `hint`; die Prüfliste meldet solche Fälle als `legacy_cancelled_open`).
  Danach gilt für sie, was für jede Rechnung mit Stornorechnung gilt (kein Wiederbeleben)
- **Rechnungsnummer beim ersten Speichern** (`allocate_invoice_number`, `release_invoice_number`,
  `peek_next_number` in `db/numbering.py`): Die Nummer wird in der Transaktion des Speicherns vergeben – auch
  als Entwurf, bei Angebot → Rechnung ebenso –, der Zähler rückt dort vor (`UPDATE` + `SELECT` in der
  `BEGIN IMMEDIATE`-Transaktion, kein `RETURNING`: ältere SQLite-Versionen kennen es nicht); scheitert das
  Speichern, ist sie nicht verbraucht. Der Vorschlag der Maske ist genau diese Nummer (Formatvorlage und Zusatz
  des Kreises, eine belegte Nummer wird übersprungen). **Die Nummer im Feld zählt nur als Vorschlag, solange
  sie unverändert bleibt:** Die Maske schickt ihn mit (`numberSuggestion`); ist die Nummer beim Speichern genau
  dieser Vorschlag, vergibt der Server frisch – ein zweites Fenster kann ihn verbraucht haben – und nennt die
  vergebene Nummer in der Antwort (`invoice_number`), die Maske zeigt sie. **Vergeben wird nach dem Jahr des
  Rechnungsdatums:** Gibt es für das Jahr keinen Kreis, entsteht er (Buchstabe, Zusatz, Vorlage des jüngsten).
  Eine eigene Nummer, die zur Vorlage eines Kreises passt, hebt dessen Zähler, nie nach unten. Statuswechsel und
  spätere Änderungen zählen nie. **War die Rechnung schon einmal versendet** (`Database.was_sent_before`), ist die
  Nummer auch im Entwurf gesperrt (409 „Die Rechnung war bereits versendet – ihre Nummer bleibt.“; die Maske zeigt
  das Feld `readonly` mit Hinweis; eine Nummer, die sich nur durch Leerraum unterscheidet, bleibt unberührt und wird
  nicht geschrieben). `invoiceId` liest `/invoice/save` als Ganzzahl (`"1"` und `1.0` gelten, alles andere – `"01"`,
  `1.5`, `true` – ist 400): Der Ordner `Fassungen/<ID>` heißt nach der Zahl. Eine Formatvorlage braucht **genau einen Zähler** (`{n}`, `{nn}` oder `{nnn}`,
  `number_format_error`): Beim Speichern eines Nummernkreises wird sie sonst mit Klartext abgelehnt; ein
  Altbestand mit anderer Vorlage lässt den Vorschlag leer (Hinweis in der Maske, `peek_next_number` wirft nie)
  und das Speichern mit dem Klartext `NumberRangeError` scheitern, nie die Seite. **Angebote (seit 2.8.5) bekommen ihre Nummer auf
  demselben Weg** (`allocate_document_number(conn, 'quote', Datum, requested)`; `allocate_invoice_number` ist dieselbe Funktion für
  `'invoice'`): Der Kreis der Art `quote` entsteht beim ersten Speichern (Buchstabe `A`, `DOCUMENT_LETTERS`; bis 2.8.4 gab es keinen,
  die Maske schlug immer `<JJ>A001` vor, und das zweite neue Angebot scheiterte), er steht unter Stammdaten → Nummernkreise
  („Angebote“). Der Vorschlag (`peek_next_number('quote', Jahr)`) ist genau die Nummer, die das Speichern vergibt – auch ohne Kreis des
  Jahres –, die Maske schickt ihn als `numberSuggestion` mit; die Antwort von `/quote/save` bleibt `{success, quote_id}` (die Maske
  lädt das Angebot danach neu). Das Löschen eines Angebots gibt die Nummer nicht zurück (eine Lücke bleibt), das Ändern fasst den
  Zähler nie an. `tests/test_quote_numbering.py`
- **Entwurf löschen** (`POST /invoice/delete?id=`, `Database.delete_draft_invoice`): nur Status `draft`, ohne
  zugeordnete Zahlung und ohne Forderungsbuchung, in einer Transaktion; eine Ablehnung geht mit dem Grund
  (`err`) zurück auf die Rechnung. Ein Quellangebot im Status `converted` ohne weitere Rechnung geht auf
  `accepted` zurück. PDF und XML des Entwurfs räumt `handlers.handle_delete_invoice` danach weg – nur im
  Dokumentordner, nur wenn kein anderer Beleg darauf verweist und nie etwas in „Fassungen“
  (`remove_document_file`). Ein abgeschlossener Entwurf wird erst auf Entwurf
  zurückgesetzt. **Angebot → Rechnung** (`convert_quote_to_invoice`) geht einmal je Angebot und nur aus
  Entwurf/versendet/angenommen (`UPDATE … WHERE Status IN (…)` in einer Transaktion; Klartext bei der Ablehnung).
  Ein Angebot **bleibt „umgewandelt“, solange es eine Rechnung daraus gibt**: `/quote/status`
  (`set_quote_status`) und `/quote/save` (`QUOTE_STAYS_CONVERTED_SQL` im `UPDATE`) lassen den Status nicht
  verlassen – sonst ließe es sich auf „angenommen“ setzen und ein zweites Mal umwandeln
- **Fassungen (`export/invoice_files.py`):** Geht eine versendete Rechnung zurück auf Entwurf/abgeschlossen oder wird
  sie berichtigt, **verschiebt** `archive_sent_documents` PDF und XML nach
  `invoices/<Jahr>/Fassungen/<ID>/<bisheriger Name> – versendet bis <JJJJ-MM-TT>.<pdf|xml>` – nie überschreiben
  (ein vergebener Name bekommt einen Zähler), nie löschen, nur Dateien im Dokumentordner, die kein anderer Beleg
  führt (ein umgebogener Pfad außerhalb bleibt, wo er ist; was schon in einem Ordner „Fassungen“ liegt,
  wird nie weiterverschoben; zeigt der gespeicherte Pfad woandershin, wird zusätzlich die Datei am
  Namensschema abgelegt), `PDFPath`/`XMLPath` werden geleert. Beim Zurückholen zählt `DocumentVersion` um
  eins hoch – nur, wenn eine Datei abgelegt wurde –, und `InvoiceDocumentVersions` hält Fassung, Pfad und
  SHA-256 je Datei fest (Anlass `reopened`, beim Berichtigen `corrected`). Scheitert ein
  Verschieben, geht das Verschobene zurück und das Zurückholen wird abgelehnt (auch ein scheiternder Commit legt
  es zurück). Erneutes Versenden erzeugt eine neue Datei am Namensschema; die Fassung bleibt. **„War schon
  versendet“** beantwortet `Database.was_sent_before` (auch in einer Transaktion): `SentAt` ist gesetzt, ODER
  das Protokoll kennt einen Statuswechsel aus einem versendeten Status, ODER – Bestand aus 2.1.2 – eine Datei
  liegt in `Fassungen/<ID>` in irgendeinem Jahresordner (`export.invoice_files.was_sent_before`):
  `delete_draft_invoice` gibt die Nummer dann **nie** frei (die Lücke bleibt; auch nicht bei einer versendeten
  Rechnung ohne abgelegte Datei), und die Rückfrage beim Löschen sagt es. Der Ordner liegt unter `invoices/` und
  ist damit Teil von Datensicherung und Wiederherstellung (`tests/test_invoice_versions.py` prüft es). Die
  Rechnungsseite zeigt die Fassungen und das Protokoll im Kasten „Fassungen und Protokoll“ (Tabellenzeilen und
  Dateien aus 2.1.2 ohne Zeile, die ohne Fassungsnummer erscheinen)
- **Dateinamen-Kollision:** Gehört der Zielpfad der abgelegten Datei eines anderen Belegs (zwei Nummern, die zum
  selben Namen werden), bekommt der Name die ID als Zusatz (`<Nr.> <Kunde> (ID n).pdf`,
  `invoice_file_path(…, db)`); der Rückfall über das Namensschema nimmt nie die Datei eines anderen Belegs.
  **Das Dokument zeigt nie den Stand der Zahlungen** (`prepaid` ist immer 0, Zahlbetrag = Brutto)
- **Heruntergeladen** wird immer die abgelegte Datei (`/invoice/pdf_download`,
  `/invoice/xml_download`) – nie eine frisch erzeugte. Fehlt sie, kommt 404 mit
  kurzem Klartext, den das Frontend direkt anzeigt. Die Fassungen liefert `GET /invoice/version_download?id=&file=&year=`,
  rein lesend: `file` ist ein reiner Dateiname (kein Trenner, kein `..`), gefunden wird nur eine `.pdf`-/`.xml`-Datei
  in einem Ordner `Fassungen/<id>` genau dieser Rechnung (`path_in_documents`); `year` (Jahresordner, nur ASCII-Ziffern;
  die Links des Kastens tragen es immer) trennt gleiche Dateinamen in zwei Jahresordnern – die Datei muss in genau diesem
  Ordner liegen, ohne `year` gilt der erste Treffer; alles andere – fremde ID, Angebot, Pfadanteil, ungültiges oder
  leeres `year` – ist 404
- **Angebote** gelten genauso: `/quote/pdf_download` liefert nur die abgelegte PDF
  (`quotes/<Jahr der Erzeugung>/<Angebots-Nr.> <Kunde>.pdf`), sonst 404 „Kein PDF
  vorhanden – bitte zuerst erzeugen.“. **Erzeugt** wird nur per POST
  (`/invoice/pdf_generate`, `/quote/pdf_generate`; GET → 405) – die Oberfläche
  erzeugt zuerst und lädt dann. Der Stundenzettel (`/worktime/pdf`, GET) schreibt
  nichts auf die Platte (`generate_worktime_pdf(store=False)`)
- **Belegtyp:** Rechnungen und Angebote teilen sich die Tabelle `Invoices`; jede Rechnungs- und
  jede Angebotsroute prüft den Typ (`Database.get_document`, `invoices.document_type`, beim
  Schreiben `… AND DocumentType = …`) – die ID eines Belegs der anderen Art ergibt 404 bzw.
  „Rechnung/Angebot nicht gefunden.“ und ändert, versendet und erzeugt nichts
- **Gespeicherte Dokumentpfade** (`Invoices.PDFPath`/`XMLPath`) gelten nur innerhalb des
  Dokumentordners des Benutzers (`<Datenordner>/invoices`, bei Angeboten `quotes`;
  `export/invoice_files.path_in_documents`, aufgelöst mit `realpath`): Die Datenbank lässt
  sich per SQL-Konsole oder zurückgespielter Sicherung ändern, sonst ließe sich darüber
  jede lesbare Datei ausliefern oder löschen. Jede Stelle, die einen gespeicherten Pfad
  öffnet oder löscht (Download, E-Mail-Anhang, `drop_stale_file`), geht darüber
  (`stored_invoice_path`). Ein Pfad außerhalb gilt als nicht vorhanden (Fallback über das
  Namensschema, sonst 404) und steht nur im Server-Log, nie in der Antwort
- Im Browser läuft der Download über `appDownload()` (`server/pages.py`): Datei
  als Blob holen und per `<a download>` speichern. Kein Tab mit Server-URI –
  der wäre ohne laufenden Server wertlos. Der Dateiname kommt prozentkodiert
  im Header `X-Filename` (Umlaute!). **Ausnahme Sicherungen** (*Sicherungen herunterladen*): ein gewöhnlicher Link auf
  `/backup/download` (die Antwort trägt `attachment`), ohne `appDownload()` – ein Blob läge bei Gigabyte-Größe ganz im Speicher
  des Browsers
