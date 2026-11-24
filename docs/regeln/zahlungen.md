# Rechnungszahlung, Erstattung, Skonto, Zahlungsvorschläge, Reparatur

## Zahlungen

- Rechnungszahlung: `assign_invoice_payment` (`db/invoice_payments.py`) ist die einzige Stelle, die
  eine Zahlung Rechnung ↔ Bankbewegung zuordnet, `assign_storno_refund` die einzige, die eine Stornorechnung
  erstattet (*Stornobeleg*), `unassign_invoice_payment` die einzige, die eine Zuordnung
  entfernt (`delete_transaction` geht darüber, wenn die Bankbewegung oder ein markierter Satz fällt). Eine
  Zuordnung = eine `InvoicePayments`-Zeile + „Bank an Debitoren“ und „Wartekonto an Erlöskonto“ (ohne
  Forderung: „Erlöskonto an Bank“), erkennbar an `ParentBooking_ID` = Bankbewegung, `DocumentNumber` =
  Rechnungsnummer und `Status='invoice_payment'` (Formen: „Bank an Debitoren“ `liquid_first`,
  „Wartekonto an Erlöskonto“ `transfer`, „Erlöskonto an Bank“ `purpose_first`; die Forderung ist
  `transfer`; ein eindeutiger Index auf `InvoicePayments(InvoiceID, BookingID)` verhindert das Paar
  doppelt – fehlt er, weil ein Bestand Dubletten trägt, startet die App trotzdem und die Prüfliste
  meldet sie). Entfernt werden nur diese markierten Sätze, nie Sätze
  mit `SourceGroup` und nie unmarkierte (Altbestand, WISO). Prüfung, Schreiben und Neuberechnung laufen
  in einer `BEGIN IMMEDIATE`-Transaktion (der Index fängt zusätzlich ab: `IntegrityError` heißt „bereits verknüpft“); Konten, die `resolve_revenue_coa` anlegen
  könnte, werden vorher in eigenen Verbindungen aufgelöst – nie innerhalb. Die inneren Bausteine, auf denen
  beide Wege und der Umzug beim Versenden einer Ersatzrechnung aufsetzen: `_assign_in_tx(tx, …)` (der
  Transaktionsteil des Zuordnens, committet nie; `allow_cancelled` öffnet eine stornierte Rechnung mit positivem
  Betrag – nie eine Stornorechnung – und gilt allein für den Rest, der beim Umzug an der stornierten Rechnung bleibt)
  und `_unassign(conn, …)`. Nur Zahlungseingänge
  (Betrag > 0) auf versendete Rechnungen (`PAYABLE_STATUSES`), Bankkonto mit SKR-Nummer; frei ist
  `Betrag − MAX(Zuordnungen, bankwirksame Sätze)` (`_free_minor_sql`, überall dieselbe Formel – sie
  rechnet Spiegel roh, die Split-Summe dagegen über `bank_effect`, der einen Spiegel mit bekannter
  Lesart nach seiner Position zählt; beides ist bewusst nicht dasselbe). **Der freie Rest eines Ausgangs (Erstattung)
  rechnet über `bank_effect`** (`_outgoing_free_minor`, in Python): `|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze auf das
  Konto der Bewegung)` – wie die Split-Summe, anders als `_free_minor_sql` für Eingänge, das einen Spiegel roh rechnet
  (bewusst): Der Import-Spiegel „1810 an 1800“ mit +50 unter einem Ausgang über 50 (`mirror_coa`) verbraucht ihn, die
  Umbuchung ist keine Erstattung. Zuordnungen je Bewegung, Sätze je Bewegung und das Konto der Bewegung kommen aus wenigen
  gesammelten Abfragen (die Kandidatenliste `get_unallocated_outgoing_bookings` wächst linear). Der
  Zuordnungsbetrag wird auf Cent gerundet (halb auf), bevor er gekappt wird; die Steuer eines Teils ist
  anteilig (Cent), der schließende Teil bekommt den Rest der Rechnungssteuer abzüglich der Steuer der
  anderen Zuordnungen (markierte Sätze: die gebuchte, sonst – Altbestand, WISO – die anteilige).
  **Erlöskonto:** Es ergibt sich aus der Rechnung (`resolve_revenue_coa`) und wird NUR aus den markierten
  Sätzen mit positivem Betrag gelernt (die einer Erstattung lehren nichts), und nur in der Form, in der die App sie
  schreibt (Umbuchung „Wartekonto an Erlöskonto“: `COA` ist ein Wartekonto, gelernt wird das Gegenkonto; Einzelsatz „Erlöskonto an Bank“: das Gegenkonto
  ist ein Zahlungskonto, gelernt wird `COA`; der jüngste gewinnt) – was ein Nutzer im Split-Editor in
  eine andere Form gebracht hat, lehrt nichts. Es gilt nur als Erlöskonto 4000–4999 ohne Wartekonten,
  sonst 4400/4300 und für § 19 4185 bzw. ab 2026 4184 (*Kleinunternehmer-Erlöse*). Das Konto der Bankzeile (Anzeige-Backfill) und Sätze des WISO-Imports lehren
  nichts – es kann ein Bank-, Aufwands- oder Wartekonto sein. Ist für den Satz (z. B. 5 %, 16 %)
  keines hinterlegt, wählt der Nutzer es (`revenue_coa_id`): ein Erlöskonto, in der DB-Schicht
  geprüft, sonst Ablehnung; `suggest_revenue_coa` ist nur die Vorauswahl, nie eine Entscheidung.
  Status und Rest berechnet `next_payment_status` (nie zurück auf „abgeschlossen“, „storniert“
  bleibt; „Rest unter einem Cent gilt als beglichen“ steht in `money.is_covered`).
  **Die Forderung (`10000 an Wartekonto`) ist nie Kind einer Bankbewegung:** `matching.receivable_condition`
  (10000 im SOLL – `COA` = 10000 bei Betrag > 0 oder Gegenkonto = 10000 bei Betrag < 0, bei einer
  Gutschrift `COA` = 10000 mit negativem Betrag und der Beleg-Nr. einer Rechnung mit negativem Brutto –
  und keine Seite ein Zahlungskonto) schließt sie aus Auto-Abgleich und Editor-Kandidaten aus,
  `delete_transaction` hängt sie ab statt sie zu löschen. Was 10000 im Haben führt (Erlösminderung,
  Skonto, Forderungsverlust „… an 10000“), ist keine Forderung und geht mit seiner Bewegung. Alle
  Stellen suchen die Forderung über diese Bedingung (auch `assign_invoice_payment`,
  `get_receivable_booking`). In der Buchungsliste trägt die Forderung (eigenständige Zeile) seit 2.6.6 das graue Abzeichen „Forderung“
  statt des Hakens „Buchung vollständig gebucht“ und einen ungefärbten Betrag – sie ist kein Geldeingang und noch keine Einnahme
  (`Database.receivable_entry_ids`, EINE Abfrage je Seite für alle eigenständigen Sätze: die Einzelzeilen und die Mitglieder der Gruppen, nie die
  Sätze unter einer Bankbewegung). Seit 2.7.0 gilt das auch für die Gruppe „Split n×“ – eine Rechnung mit mehreren Steuersätzen bucht je Satz eine
  Forderung, die Liste fasst sie zusammen: Sind ALLE Mitglieder Forderungen, trägt die Kopfzeile das Abzeichen statt des Hakens, keinen grünen
  Hintergrund (`row-ok`) und einen ungefärbten Betrag, und jede Kindzeile einer Forderung trägt das Abzeichen. Eine gemischte Gruppe behält ihre
  Kopfzeile, nur ihre Forderungs-Kindzeilen tragen das Abzeichen; was unter einer Bankbewegung steht, bleibt, wie es war
  (`tests/test_receivable_row.py`). Stufe 7 des
  Auto-Abgleichs setzt sie erst auf `resolved`, wenn die Summe der Sätze „… an 10000“ mit gleicher
  Beleg-Nr. den Forderungsbetrag erreicht – seit 2.6.8 ist das die Summe ALLER eigenständigen Forderungszeilen der Beleg-Nr.
  (`COA` = Debitoren, ohne Bankbewegung, auch die schon `resolved`; eine Rechnung mit mehreren
  Steuersätzen bucht je Satz eine; **eine Forderung ist dabei nur, was `receivable_condition` so nennt UND Debitoren als `COA` führt** – nicht „10000 an Kasse“ (ein Zahlungskonto auf einer Seite)
  und nicht „10000 an X“ mit negativem Betrag ohne Gutschrift-Rechnung (10000 steht dort im Haben; mit ihr in der Summe gälte 119,00 − 69,00 = 50,00 € als von 50,00 € Teilzahlung gedeckt),
  Gutschrift und Gegenbuchung einer Stornorechnung bleiben drin –, und **Debitoren sind ALLE Konten mit der Nummer 10000** (zwei Kontenrahmen können eines führen;
  Stufe 7 nahm bisher nur das erste); dieselbe Auswahl an beiden Stellen – `_after_payment_removed` über `_standalone_receivable_rows`, Stufe 7 liest sie für alle Nummern in EINER Abfrage, weil `DocumentNumber` keinen Index hat): Sie werden gemeinsam
  `resolved` (der Zähler `resolved` des Abgleichs zählt die geänderten Zeilen), nie eine für sich – die kleinere gälte nach einer
  Teilzahlung sonst zu früh als ausgeglichen –, und `unassign` (`_after_payment_removed`) setzt sie gemeinsam zurück, sobald die
  Summe nicht mehr gedeckt ist; bei genau einer Zeile ist alles wie vorher (`tests/test_receivable_resolution.py`). **Gelesen wird robust:** Die Beträge der Forderungszeilen und der Zahlungen
  stehen einzeln und roh da und werden in Python summiert (`matching.receivables_covered`: ganze Zahlen – auch `1190000.0` – und NULL (= 0) zählen, Text, Blob, ein Bruchteil von Minor Units oder ein
  Wahrheitswert ergeben keine Antwort: kein Statuswechsel, kein Fehler; eine SQL-Summe bräche bei mehr als 2^63 mit „integer overflow“ ab, und mit ihr der ganze Abgleich), die Beleg-Nr. wird als BLOB gelesen
  und nach ihren Bytes gruppiert (ungültiges UTF-8 bricht nichts ab), und die Verbindung des Laufs schließt sich auch bei einem Fehler (`link_bank_to_entries` ist die Hülle –
  Commit, bei jeder Ausnahme Rollback – um `_link_bank_to_entries_in`). Die Forderung geht mit der
  letzten Zahlung, wenn der Status der Rechnung kein Forderungsstatus ist (`RECEIVABLE_STATUSES`, z. B.
  eine von Hand stornierte Rechnung: Altbestand oder ohne positiven Betrag) – gleicher Endzustand, egal ob erst storniert oder erst
  entfernt wird; **entfernt wird nur die Forderung, die die App selbst gebucht hat** (`_receivable_removal`:
  ohne `SourceGroup`, Wartekonto als Gegenkonto, keiner Bankbewegung zugeordnet), nie eine importierte oder von
  Hand gebuchte. **Ausnahme Storno-Paar:** Hat die Rechnung eine Stornorechnung (oder ist sie eine), bleiben die
  Forderung der Ursprungsrechnung und die Gegenbuchung der Stornorechnung in jedem Fall stehen
  (`_drop_receivable`, auch für `remove_receivable_booking`) – was mit den Zahlungen geschieht, ändert daran nichts.
  Negative Zuordnungen kennen der WISO-Import (`link_invoice_to_transaction`, mit Vorzeichen, ohne
  Sätze) und die Erstattung einer Stornorechnung (`assign_storno_refund`, mit Sätzen).
  `find_payment_inconsistencies` prüft nur lesend und linear im Bestand (Seite *Daten*:
  nur auf Klick, `?check_payments=1`, nie beim Laden; Hinweis auf der Rechnungsseite); Zuordnungen auf
  Nicht-Bankbewegungen oder mit `SourceGroup`-Sätzen (WISO) löst `no_settlement`/`transfer_mismatch`
  nicht aus. Zwei Kategorien gelten dem Storno-Paar und zählen nur Forderungen, die die App selbst gebucht hat
  (`_app_receivable_ids`; ein Import löst keinen Fehlalarm aus): `storno_counter_mismatch` (von Forderung der
  Rechnung und Gegenbuchung der Stornorechnung steht nur eine) und `legacy_cancelled_open` (von Hand storniert,
  keine Stornorechnung, die Forderung steht noch); ein intaktes Paar, auch mit Guthaben oder Erstattung, ergibt keinen Befund.
  Dazu `refund_exceeds_credit` (Σ Zuordnungen beider Belege < 0: mehr erstattet als gezahlt – kein Weg der
  Oberfläche lässt es entstehen; die Kategorie meldet, was daran vorbei geschieht: SQL-Konsole, Sicherung, Bestand älterer
  Versionen und ein Import, der nach einer Erstattung eine negative Zuordnung an die stornierte Rechnung hängt
  (`link_invoice_to_transaction` prüft das nicht); je Rechnung vorab zusammengefasst).
  **Drei Kategorien gelten den Sätzen eines Belegs** (seit 2.7.0, `_rate_findings`, nur lesend, je Art eine Abfrage für alle Belege): `rate_split_mismatch` – ein Beleg
  mit Steuersatz je Position (Rechnung wie Stornorechnung, jeder Status), dessen Positionen nicht zu seinen Summen passen (`tax_groups.mixed_groups`; dann lässt sich nichts weiter prüfen),
  dessen Forderungszeilen der App (Satz, Betrag, Steuer; bei der Stornorechnung die negierten Gruppen) nicht genau die Satzgruppen buchen, oder der ganz bezahlt ist (Σ Zuordnungen = Brutto ≠ 0),
  ohne dass je Satz sein Betrag und seine Steuer auf die Erlöskonten umgebucht wären (die markierten Umbuchungen, dieselben Sätze, die `_mixed_booked` zählt); `rate_refund_mismatch` – ein Storno-Paar, bei dem
  die stornierte Rechnung oder die Stornorechnung das Kennzeichen trägt, dessen Stand je Satz sich lesen lässt (`booked_by_rate`: derselbe Kern wie `_mixed_booked`, für alle Paare aus je einer Abfrage; ein
  unlesbarer Stand, auch Positionen, die nicht zu den Summen passen, ergibt keinen solchen Befund) und bei dem je Satz mehr erstattet ist als gezahlt oder Betrag und Steuer nicht aufgehen (`tax_groups.booked_fault` am
  Topf der Zahlungen, `refund_pool` – derselbe Stand, den der Schutz beim Entfernen einer Zahlung verhindert): Die Summe kann aufgehen, `refund_exceeds_credit` schweigt dann; ein Befund je Paar, an der
  stornierten Rechnung, mit den Werten je Satz (gezahlt – erstattet); und `unflagged_rate_mix` – ein
  Beleg OHNE das Kennzeichen, dessen Positionen verschiedene Sätze tragen (verglichen nach `rate_pct`: 0,19 ist 19; eine Position ohne Satz zählt nicht, ein Kopf unter 0, der Platzhalter für „ohne Steuerausweis“,
  nie, ein Angebot nie): Er wird mit EINEM Satz geführt, und seine Buchungen rechnen mit dem Satz im Kopf – ein Bestand aus einer Version vor 2.7.0 kann so etwas tragen; es wird genannt, nie umgedeutet.
  Ein Beleg mit einem Satz ist von keiner der drei betroffen (`tests/test_single_rate_snapshots.py` hält es fest); ein Betrag, der keine ganze Zahl ist, zählt nicht mit (der Befund nennt, was lesbar ist).
  **Diese Forderungen liest die Prüfliste einmal für alle Paare und Alt-Stornos** (`_app_receivable_amounts`, mit
  Beträgen; dieselbe Bedingung `receivable_condition` und dieselbe Einordnung `_is_app_receivable` wie
  `_app_receivable_ids`, das darauf aufsetzt) – je Rechnung nachzuschlagen wäre quadratisch, `DocumentNumber` hat keinen
  Index; nur der Filter auf eine Rechnung (`invoice_id`) liest gezielt. Die Reparatur je Rechnung ist *Zuordnung neu
  aufbauen* (nächster Punkt)
- **Zuordnung neu aufbauen (seit 2.5.13, `db/invoice_repair.py`, `Database.rebuild_invoice_payments`; `POST /invoice/rebuild-payments`,
  JSON `{invoiceId[, execute, check, revenueCoaId]}`):** Die Reparatur der Zahlungszuordnung **einer** Rechnung (es gibt keine
  Sammelreparatur), nur nach einem Probelauf und mit Bestätigung. Sie macht vier Dinge, in dieser Reihenfolge: (A) doppelte Zuordnungen
  desselben Paares (Rechnung, Bewegung) mit gleichem Betrag zusammenfassen – die kleinste ID bleibt, verschiedene Beträge nur melden –,
  (B) die als Kind an einer Bankbewegung hängende Forderung der App (genau eine, `_is_app_receivable`) lösen (`ParentBooking_ID` leer, ein
  Status `resolved` zurück – wie `remove_split_row`), (C) jede Zuordnung ohne eigene Sätze mit den heutigen Bausteinen neu schreiben
  (`_unassign` + `_assign_in_tx`, **in zwei Phasen in EINEM Savepoint:** erst alle Kandidatinnen entfernen, dann in der Reihenfolge der
  Bankbewegungen (ihr Datum, dann die ID der Zuordnung) neu buchen – so verteilt sich die Steuer wie beim Zuordnen nacheinander: die späteren
  zählen noch nicht als bezahlt, die letzte, die die Rechnung schließt, bekommt den Rest, und nicht angefasste Zuordnungen zählen als schon
  bezahlt (das bisherige Zahlungsdatum einer Zuordnung bestimmt die Reihenfolge nicht). Geht eine
  nicht mit genau ihrem Betrag, nimmt der Savepoint den ganzen Durchgang zurück (Zuordnungen, Sätze, Rechnung und die Einträge fürs Replay-Log),
  sie bleibt samt ID liegen und wird als `not_free` gemeldet, und die übrigen laufen ohne sie noch einmal – höchstens so viele Durchgänge, wie
  es Kandidatinnen gibt. Eine Notiz der alten Zuordnung bleibt; das Zahlungsdatum der neuen ist das Datum der Bankbewegung, und weicht das
  bisherige ab, nennt es der Text der Aktion – mit dem alten Wert nur, wenn der ein ISO-Datum ist), (D) den offenen Betrag neu rechnen – **der Status bleibt** (eine von Hand auf
  „bezahlt“ gesetzte Rechnung bleibt es), nur nach A rechnet `next_payment_status` ihn mit dem Tag `today` neu. **Es wird kein Buchungssatz
  gelöscht oder geändert** (B ist die eine Ausnahme: nur `ParentBooking_ID`/`Status` der Forderung); neue Sätze kommen nur dazu, mit Datum und
  Steuerdatum der Bankbewegung und der Marke. `_booking_snapshot_unchanged` prüft es mechanisch: Die vorhandenen Sätze der Rechnung, ihrer
  Bewegungen und deren Kinder müssen nach dem Schreiben Spalte für Spalte gleich sein (die gelöste Forderung ausgenommen) – sonst nimmt die
  Reparatur alles zurück und meldet es (409, `REBUILD_UNSAFE_MESSAGE`). **Ein zweites Netz** liest die Prüfliste nach dem Schreiben noch einmal:
  Nennt sie eine Kategorie, die sie vorher nicht nannte (etwa `transfer_mismatch`, wenn ein Erlös doppelt gebucht würde), geht ebenso alles
  zurück (409, `REBUILD_UNSAFE_MESSAGE`); die Kategorien stehen nur im Server-Log, nie in der Antwort. Storno-Paare (die Rechnung hat eine Stornorechnung oder ist eine),
  Rechnungen außerhalb von versendet/überfällig/Teilzahlung/bezahlt und ohne positiven Betrag bekommen nur den neu gerechneten offenen Betrag.
  **Ein Beleg mit Steuersatz je Position** (`MixedTaxRates`, seit 2.7.0) gehört dazu (Grund `mixed`, vor `storno_pair`): A bis C laufen für ihn nicht – R1 verlangt EIN Erlöskonto und genau EINE Forderung der App,
  und je Satz zu verallgemeinern wäre eine neue Regel (Etappe 3, `docs/aufgaben.md`; die Bedingungen von C werden nicht gelockert) –, D rechnet nur den offenen Betrag aus den Zuordnungen neu (Probelauf wie
  Ausführung, kein Satz wird geschrieben); die Reparatur sucht und wählt für ihn nie ein Erlöskonto (`revenueCoaId` gilt dort nie, `needs_revenue_coa` kommt nie), und die Seite zeigt keine Auswahl.
  **Bedingungen für C („R1“), alle zugleich:** die Zuordnung hängt an einer Bankbewegung mit Zahlungseingang; unter der Bewegung steht kein Satz
  mit `SourceGroup` (Import: still übergangen) und keiner mit der Nummer dieser Rechnung (außer der in B gelösten Forderung); jeder andere
  Satz trägt die Nummer einer anderen Rechnung, die selbst der Bewegung zugeordnet ist; die Forderung der Rechnung ist genau eine und von der
  App; das Bankkonto hat eine SKR-Nummer; der Betrag hat keinen Bruchteil eines Cents; für den Steuersatz ist ein Erlöskonto hinterlegt oder
  gewählt (`resolve_revenue_coa(…, create=False)` – **die Reparatur legt nie ein Konto an**, auch der Probelauf nicht; fehlt es, kommt
  `needs_revenue_coa` vor dem ersten Schreiben). **Dazu gilt für die ganze Rechnung „nichts an anderer Stelle“** (`_rows_elsewhere`, gelesen vor
  jeder Kandidatin; Grund `elsewhere`, einmal je Rechnung; A, B und D laufen weiter): Es gibt keinen Buchungssatz, der keine Bankzeile ist, die
  Nummer der Rechnung trägt (ohne Leerraum am Rand – alles, was `str.strip()` entfernt, auch Tabulator, Zeilenumbruch und geschütztes
  Leerzeichen: `_EDGE_WHITESPACE` – und ohne Rücksicht auf die Schreibweise: `LOWER(TRIM(…, _EDGE_WHITESPACE))`), nicht ihre Forderung ist und nicht
  unter einer Bankbewegung steht, der diese Rechnung zugeordnet ist – also keinen eigenständigen Satz (auch nicht auf der Kasse), keinen unter
  einer fremden Bewegung und keinen mit `SourceGroup` –, und die Prüfliste nennt für die Rechnung keinen Befund `orphan_payment_rows`; sonst
  stünde der Erlös nach dem Neuaufbau zweimal in den Büchern. Der Grund steht nur im Plan, wenn dadurch eine Zuordnung nicht aufgebaut wird.
  **Und die vorhandenen Sätze buchen nicht mehr Erlös, als die übrigen Zuordnungen erklären** (Erlös = Betrag der Sätze, die auf einer Seite ein
  Erlöskonto führen, ohne Wartekonten: „Wartekonto an Erlöskonto“ und „Erlöskonto an Bank“ je einmal, „Bank an Debitoren“ nie; ein Betrag, der keine
  ganze Zahl ist, sperrt): **je Rechnung** (`_overbooked`, Grund `overbooked`, einmal je Rechnung; A, B und D laufen weiter) liegt der Erlös aller Sätze
  mit ihrer Nummer (außer der Forderung) nicht über der Summe ihrer Zuordnungen, die bleiben – nicht zusammengefasst, nicht neu gebaut –, **je Bewegung**
  (`_overbooked_movement`, Grund `overbooked_movement`) der Erlös der Sätze unter ihr (sie gehören alle anderen zugeordneten Rechnungen) nicht über der
  Summe von deren Zuordnungen dort; sonst stünde der Erlös nach dem Neuaufbau zweimal in den Büchern.
  **Diese Bedingungen nicht lockern – lieber melden als falsch reparieren.** **Grenze:** Einen Satz ohne die Rechnungsnummer, der denselben Erlös
  schon bucht, erkennt die Funktion nicht – deshalb zeigt der Probelauf die Wirkung auf die EÜR, und der Hinweis im Plan bittet um die Prüfung.
  Was nicht angefasst wird, steht mit festem Grund in `skipped` (`REBUILD_SKIP_REASONS`). **Probelauf und Ausführung sind derselbe Code** (`_rebuild_in`,
  EINE `BEGIN IMMEDIATE`-Transaktion): Der Probelauf endet mit ROLLBACK und hinterlässt nichts – keine Zeile, keinen Zähler (`sqlite_sequence`),
  keinen Eintrag im Replay-Log (der folgt wie sonst erst dem Commit); die Ausführung schreibt nur mit `confirmed_check` = `RebuildPlan.check`,
  der SHA-256-Prüfsumme über Aktionen, neue Sätze (alle Spalten, nie ihre IDs), Wirkung auf die EÜR, Status, offenen Betrag, Befunde vorher
  und nachher, Gründe, Erlöskonto und – nur wenn Dubletten zusammengefasst werden, denn nur dann hängt der Status vom Tag ab – den Tag; nie an
  `UpdatedAt`. Hat sich der Bestand seither geändert (oder fehlt die Bestätigung), kommt
  409 mit dem frischen Plan, und nichts ist geschrieben. Der Plan nennt die Wirkung auf die EÜR je Datum und Erlöskonto (netto, Umsatzsteuer)
  und fest den Hinweis, dass ein schon gemeldeter Zeitraum dort nachzutragen ist, und die Bitte, vorher zu prüfen, dass Zahlung und Erlös nicht
  schon an anderer Stelle gebucht sind (keine Steuerberatung); die Seite zeigt ihn nur, wenn der Plan neue Buchungssätze enthält (der Knopf
  „Neu aufbauen“ steht bei jeder Aktion). Die Route (`_JSON_PATHS` +
  `_POST_ONLY_JSON_PATHS`, GET → 405) liest die ID über `_json_positive_id`, `execute` als Wahrheitswert, `check` als 64 Hexziffern (nur mit
  `execute`) und `revenueCoaId` streng; ein unerwarteter Fehler ist 500 mit `REBUILD_FAILED`, nie ein Ausnahmetext. Die Seite der Rechnung
  zeigt Knopf und Plan unter der Befundliste (nur mit Befund, nicht an einer Stornorechnung; das Skript setzt Werte nur über `textContent`);
  *Daten* verweist darauf. `tests/test_invoice_payment_repair.py`
- **Kleinunternehmer-Erlöse (seit 2.8.2, `db/invoice_payments.py`):** Das Erlöskonto eines Belegs ohne Steuerausweis (Kopfsatz −1, § 19) hängt am
  Datum der **Bankbewegung** der Zahlung bzw. Erstattung – nicht am Rechnungsdatum: bis 31.12.2025 **4185**, ab 1.1.2026 **4184**
  (`small_business_revenue_number`, Stichtag `SMALL_BUSINESS_SWITCH`; der Kontenrahmen ab 2026 führt 4185 nicht mehr). Es gilt nur ein ISO-Datum
  `JJJJ-MM-TT`; sonst lehnt die Zahlung vor dem Auflösen der Konten ab (`REVENUE_DATE_UNREADABLE`). Ohne Datum (Anzeigen) gilt heute
  (`_revenue_today`, in Tests festsetzbar). **Die Entscheidung, wer der Regel folgt, steht allein in `uses_small_business_rule`:** nur der
  Platzhalter −1; Satz 0 mit Steuerausweis bleibt fest auf 4185 (wie bisher). **Gelernt:** Ein gelerntes 4184/4185 im Topf „0 % und § 19“ zählt nicht
  als gelernt (`resolve_revenue_coa(tax_rate, create, on)`: beide sind Standard der App, keine Wahl des Nutzers); ein anderes gelerntes Konto (4120)
  gilt in beiden Jahren. **Zahlung:** `_payment_prerequisites(on=)`/`payment_precheck(on=)` (die Maske nimmt ihr Datumsfeld); der Kontext trägt
  `revenue_date_number`, und `_assign_in_tx` lehnt mit `PAYMENT_CHANGED_MESSAGE` ab, wenn das Datum der Bewegung in der Transaktion zum anderen
  Konto gehört. **Eine abgelehnte Zuordnung legt kein Konto an:** Betrag ≤ 0, Zahlungsausgang (`INCOMING_ONLY_MESSAGE`) und ausgeschöpfte Bewegung
  (`NOTHING_TO_ASSIGN_MESSAGE`) werden VOR den Vorbedingungen geprüft (sie legen 4184/4185 bei Bedarf an, nie in einer Transaktion). **Erstattung:**
  mit markierten Sätzen das Konto der Zahlungen (Teilzahlungen über den Jahreswechsel liegen auf 4185 und 4184: die vorhandene Wahl des Nutzers,
  `MIXED_REVENUE_MESSAGE`), ohne (Import) das Konto nach dem Datum der Erstattung. **Umzug auf die Ersatzrechnung:** `_payment_move_context` löst
  je benötigter Nummer ein Konto auf (`revenue_by_number`, nur was eine Zahlung braucht); jede Zahlung zieht auf das Konto ihres Datums um, liegt
  sie nicht darauf (eine 2026er Zahlung, die 2.8.1 auf 4185 gebucht hat), bleibt sie (`MOVE_SKIP_REASONS['revenue_account']`: „… nicht das für
  Steuersatz und Zahlungsdatum hinterlegte“), bei unlesbarem Datum `revenue_date`; wechselt der Kopf der Ersatzrechnung zwischen Vorbereitung und
  Transaktion vom Platzhalter auf 0 %, lehnt `CHANGED_MESSAGE` ab. **Zuordnung neu aufbauen:** § 19 hat dort nie eine Wahl (die Seite zeigt keine
  Auswahl, `needs_revenue_coa` kommt nie); je Zuordnung das Konto ihres Datums (`RebuildCandidate.revenue_coa`, `create=False`), fehlt es, bleibt
  sie liegen (`revenue_missing`: „… Standardkonten ergänzen“; `revenue_date` bei unlesbarem Datum). Die Bedingungen von Umzug und Neuaufbau sind
  nicht gelockert. **Bestand:** Zahlungen ab 2026, die eine frühere Version auf 4185 gebucht hat, bleiben, wie sie sind (kein Befund, keine
  Umbuchung). Der Standard-Kontenrahmen führt 4184 „Steuerfreie Erlöse Kleinunternehmer nach § 19 Abs. 1 UStG“; ältere Bestände bekommen es über
  „fehlende Standardkonten“ oder bei der ersten Zahlung. Keine Auswertung hat eine Sonderregel (4184 liegt in denselben Bereichen wie 4185). Das
  Demo-Profil `test` hat genau zwei Zahlungen auf 4184 (Januar 2026); die Schnappschüsse `demo_2026`, `demo_2026_full` (EÜR-Kasten) und
  `demo_db_2026` (DATEV) sind dafür neu genommen – nur Nummer und Name einer Zeile bzw. das Gegenkonto zweier Sätze weichen ab (nachgewiesen
  Zeile für Zeile). `tests/test_small_business_revenue_4184.py`
- **Skonto bei der Zahlungszuordnung (seit 2.9.0, Schema 2009, `db/invoice_payments.py`):** Zahlt der Kunde eine Rechnung mit Skonto-Bedingungen
  bis auf höchstens den vorgesehenen Abzug, bucht die Zuordnung den Rest auf Wunsch als Skonto aus (`assign_invoice_payment(…, skonto=True)`) –
  **nur auf Klick, nie vorausgewählt**. **Speicherung:** `InvoicePayments.SkontoAmount` (Minor Units, NULL = keines; streng gelesen: nur eine
  positive ganze Zahl zählt). `Amount` bleibt das gezahlte Geld – freier Rest der Bewegung, Guthaben eines Storno-Paares und Erstattung rechnen
  unverändert damit; was an der Rechnung erledigt ist, ist `Amount` plus Skonto (`settled_sql`, die EINE Stelle: `_recalc_invoice`, `_assign_in_tx`,
  Prüfliste, Schritt D des Neuaufbaus). **Sätze** (markiert, unter der Bankbewegung, in dieser Reihenfolge; Beispiel 119,00 € / gezahlt 116,62 €):
  „Bank an Debitoren“ 119,00 (nur mit Forderung), „Wartekonto an Erlöskonto“ bzw. ohne Forderung „Erlöskonto an Bank“ 119,00 mit dem Rest der
  Rechnungssteuer, zuletzt **„Skonto-Konto an Bank“** (`purpose_first`) −2,38 mit −0,38 Steuer. Die Bankseite ergibt das Geld, und alle Leser stimmen
  ohne eigene Regel (EÜR 100,00 / −2,00 / 18,62, Debitoren und Wartekonto 0, Summen- und Saldenliste, Liquidität, Dashboard, Split-Summe, Stufe 7) –
  die Form ist an vier Kandidaten gemessen; ein Satz „Skonto an Debitoren“ liest die EÜR ohne Steuer und die Summen- und Saldenliste unzerlegt. Im
  DATEV-Stapel stehen dafür zwei Bankzeilen (119,00 und 2,38, zusammen der Betrag des Auszugs). **Konten** (`SKONTO_ACCOUNTS`): 19 % → 4736, 7 % →
  4731, 0 % und § 19 → 4730 ohne Steuer (Standardkonten; fehlt eines, legt die erste Zuordnung es vor der Transaktion an – eine abgelehnte legt
  nichts an); ein Skonto-Konto lehrt nie ein Erlöskonto, der Skonto-Satz einer Zuordnung mit Skonto zählt nicht in `_allocations_tax`, und es steht nicht in der Wahl des Erlöskontos einer Zahlung
  (`payment_revenue_choices`) und eines Stammdaten-Steuersatzes. **Ein Skonto-Satz wird nie allein am Konto erkannt – maßgeblich ist `SkontoAmount` der Zuordnung:**
  Ein Skonto-Konto kann aus einem Stammdaten-Steuersatz oder einer früheren Wahl des Nutzers auch Erlöskonto einer Zahlung sein; sein Erlössatz zählt deshalb in `_allocations_tax`,
  löst weder `skonto_mismatch` noch `SKONTO_STORNO_EDITED` aus und ist kein Skonto für `_skonto_booked` (ohne Wert in der Spalte erkennen Stornieren und `_skonto_booked` ein
  Skonto nur an einem Satz mit negativem Betrag; `tests/test_skonto_revenue_account.py`). **Regeln** (`skonto_terms`, `_skonto_state` – EINE
  Stelle für Vorprüfung und Transaktion): Skonto = der Rest nach der Zahlung (zahlt der Kunde 117,00, sind es 2,00), in ganzen Cent, höchstens
  `skonto_amount(Brutto, Prozent)`; die Frist wird gezeigt, nie geprüft; je Rechnung höchstens EIN Skonto; Skonto-Steuer = Skonto × Satz / (100 + Satz),
  EINMAL auf den Cent gerundet (`skonto_tax`). **Stufe 1:** nur Rechnungen mit einem Satz (19 %, 7 %, 0 %, § 19) – ein Beleg mit Steuersatz je Position,
  mit einem Satz aus den Stammdaten oder mit einem anderen Kopfsatz lehnt mit festem Satz ab (`SKONTO_*`). **Entfernen:** Die Zahlung mit Skonto geht
  auf jedem Weg samt Skonto-Sätzen; solange sie an der Rechnung hängt, lässt sich keine ANDERE Zahlung dieser Rechnung entfernen
  (`SKONTO_REMOVAL_REFUSED`, in der einen Prüfung aller Entfernen-Wege, `_refund_removal_blocker`) – das Skonto stünde sonst an einer Zahlung, die die
  Rechnung nicht mehr schließt. **Stornieren nimmt das Skonto zurück** (`_reverse_skonto_in`, in der Transaktion von `cancel_invoice`): drei markierte
  Sätze unter der Bewegung der Zahlung (ohne Forderung zwei), Beleg-Nr. der stornierten Rechnung, Datum = das spätere von Stornodatum und Datum der
  Bankbewegung, danach `SkontoAmount` NULL – das Guthaben ist das gezahlte Geld, die Erstattung läuft unverändert. Sind die Skonto-Sätze im Editor
  geändert, lehnt das Stornieren ab (`SKONTO_STORNO_EDITED`: erst die Zahlung entfernen und neu zuordnen). Bekannte Folge: Die Summen- und Saldenliste
  nennt `child_other_period` für die Monate zwischen Zahlung und Storno. **Umzug und Neuaufbau:** Eine Zahlung mit Skonto zieht nicht auf die
  Ersatzrechnung um (`MOVE_SKIP_REASONS['skonto']`), und „Zuordnung neu aufbauen“ rechnet für eine solche Rechnung nur den offenen Betrag (Grund
  `skonto`) – die Bedingungen beider Wege sind nicht gelockert. **Prüfliste:** `skonto_mismatch` (die Skonto-Sätze passen nicht zu `SkontoAmount`,
  oder die Spalte trägt einen ungültigen Wert). **Vorschläge:** Signal „Betrag nach Skonto stimmt“ (freier Betrag = offen − Skonto, 50 Punkte wie der
  Betrag; braucht wie er ein Namenswort oder die Nummer). **Oberfläche:** Rechnungsseite (Zeile „Skonto: 2 % = 2,38 € bis …“, je Kandidat das Häkchen
  „Rest … als Skonto ausbuchen“, nur sichtbar, wenn der Rest passt), Buchungsmaske (dasselbe Häkchen am Feld Rechnung, Feld `skonto=1`, Vorprüfung
  vor dem Schreiben), Kasten der Zahlungseingänge (Knopf „Mit Skonto zuordnen“); `POST /invoice/link-payment` liest `skonto` streng als
  Wahrheitswert, an einer Stornorechnung 400 (`SKONTO_REFUND`). Das Demo-Profil `full` hat genau ein Beispiel (`skonto_example`), `test` keines.
  Keine Steuerberatung in den Texten. `tests/test_skonto_*.py`
- **Zahlungsvorschläge (seit 2.8.1, `db/payment_matching.py` rein, `db/payment_suggestions.py` nur lesend):** Zu einem Zahlungseingang wird die
  passende offene Rechnung **vorgeschlagen – zugeordnet wird allein auf Klick** über die bestehenden Wege (`POST /invoice/link-payment`, Feld
  `invoice_id` der Maske → `assign_invoice_payment`), nichts ist vorausgewählt; nicht einführen, dass ein Speichern still zuordnet. **Vorschlag** =
  die Rechnungsnummer steht im Verwendungszweck oder im Empfänger der Bewegung ODER (freier Betrag = offener Betrag UND gemeinsames Namenswort von
  Empfänger und Kunde); ein gleicher Betrag allein ist nie ein Vorschlag. **Es zählt nur, was von der Bank kommt:** Beleg-Nr. und Kontakt der
  Bankzeile zählen bewusst nicht – eine Zuordnung trägt beides aus der Rechnung ein (`_backfill_booking_skr`), und es bleibt nach dem Entfernen
  stehen; eine falsch zugeordnete und wieder gelöste Rechnung wäre sonst der stärkste Vorschlag. Kundenname ist die Firma, sonst der Name (nie der
  Ansprechpartner neben der Firma). Nur in Euro (Währung leer oder `EUR`, auf beiden Seiten). Punkte 100/50/30, feste Begründungen (`reason_text`,
  „ · “), eine Zahlung mehr als 7 Tage vor dem Rechnungsdatum nur mit Nummer, höchstens 5 je Bewegung bzw. Rechnung. **Nummer im Text:** gesucht im
  Originaltext ohne Rücksicht auf die Schreibweise; an einer Fuge mit einem Buchstaben darf EIN Trennzeichen stehen (Leerraum, `-`, `_`, `/`, `.` –
  `R 26003`, `R-26003`), **zwischen zwei Ziffern nur dort, wo die Nummer selbst eines trägt** (`_number_parts`: `RE-2026/0042` steht in
  `RE 2026 0042`; `26001` nicht in `26.001,00`, `202601` nicht in `2026-01-15`, `R26003` nicht in `R26 003`), ein Punkt dort nur, wo die Nummer
  selbst einen Punkt trägt (`26/001` nicht in `26.001,00`); Ziffernsperre an dem Rand, an dem die Nummer selbst mit einer Ziffer beginnt oder endet
  (`R26001` steht nicht in `R260010`, wohl aber in `RE-Nr.R26001.KD-Nr.` und `1R26001`); brauchbar wie bei den Belegen (mindestens 4 Zeichen mit
  einer Ziffer, rein numerisch ab 5), höchstens 40 Zeichen, Felder auf 2000 Zeichen gekappt, linear. Diese Regeln nicht lockern – lieber ein
  Vorschlag weniger als ein falscher. **`pair_up`** ist die Eins-zu-eins-Liste der Übersicht: Paare nach (−Punkte, Datumsabstand, IDs), jede
  Bewegung und jede Rechnung höchstens einmal – zwei gleich hohe Rechnungen und Zahlungen desselben Kunden bekommen so je die zeitlich nächste –,
  `ambiguous` („mehrere möglich – bitte prüfen“) bei einem weiteren Paar gleicher Punktzahl; ein Paar, das schon in `InvoicePayments` steht, ist nie
  ein Vorschlag. **Bewegungen:** Übersicht und Rechnungsseite lesen nur Bankbewegungen mit Betrag > 0 ohne Kind und ohne Zuordnung (frei = Betrag;
  `_UNBOOKED_MOVEMENTS` liest die Zuordnungen EINMAL über `NOT IN` – `InvoicePayments` hat keinen Index auf `BookingID`, eine Unterabfrage je
  Bewegung kostete bei 20 000 unkontierten Eingängen 1,2 s statt 28 ms; ein Test zählt die SQLite-Schritte); der freie Rest nach `_free_minor_sql`
  gilt für die EINE Bewegung der Maske. Kaputte Werte (Betrag als Text, unlesbares Datum) sind kein Kandidat, nie ein Fehler. **Seiten:** Kasten
  `#paySuggest` „Zahlungseingänge zu offenen Rechnungen“ links über der Buchungsliste (aus allen Zeiträumen, höchstens 20 Zeilen, „Zuordnen“ =
  `payAssign`, lädt danach die Adresse ohne `err` neu; `refreshTable()` erneuert ihn nach einem Import; nicht im Bereich, den `openEditForm`
  austauscht), Maske einer Bankbewegung mit freiem Rest (Vorschläge zuerst in der Auswahl „Rechnung“, „Übernehmen“ setzt nur den Wert), Rechnungsseite
  (Vorschläge zuerst, Begründung unter dem Auftraggeber, der Haken bei gleichem Betrag bleibt). Scheitert eine der Abfragen, bleibt die Seite stehen
  (leer, Fehler im Server-Log). **Fehlerweg der Maske:** Scheitert die Zuordnung nach dem Speichern, bleibt der Nutzer bei der Buchung
  (303 auf `/transactions/edit?id=<ID>&err=…`, `PAYMENT_NOT_ASSIGNED`: „Die Buchung ist gespeichert, die Zahlung wurde aber nicht zugeordnet: …“),
  nicht mehr auf der Rechnung. Bekannte Grenzen: Skonto zählt nur als voller Rest bis zum vorgesehenen Abzug (seit 2.9.0, *Skonto bei der Zahlungszuordnung*), andere Teilbeträge nicht; ein Umbruch der Bank mitten in den Ziffern der Nummer ist kein Treffer; eine
  Rechnung, die eine Erlöskonto-Wahl braucht, lässt sich im Kasten nicht zuordnen (die Wahl gibt es auf Rechnungsseite und Maske); vorgeschlagen
  wird auch eine Bewegung, deren Bankkonto kein Konto im Kontenrahmen hat (der Klick meldet es). **Werte im Skript des Buchungssatz-Editors**
  (`SPLIT_BANK_TEXT`, `SPLIT_BANK_DOCNR`) stehen als JSON (`script_json`): Ein Zeilenumbruch im Verwendungszweck – der Import eines PDF-Auszugs
  verbindet Zeilen damit – ließ den Block sonst ausfallen (`tests/test_split_editor_script_values.py`). `tests/test_payment_matching.py`,
  `tests/test_payment_suggestions.py`
