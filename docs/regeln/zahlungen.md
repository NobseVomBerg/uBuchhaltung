# Rechnungszahlung, Erstattung, Skonto, Zahlungsvorschläge, Reparatur

## Zuordnung einer Zahlung

- Einzige Stellen (`db/invoice_payments.py`): `assign_invoice_payment` ordnet zu, `assign_storno_refund` erstattet eine Stornorechnung
  (siehe `rechnungen.md`), `unassign_invoice_payment` entfernt (`delete_transaction` geht darüber, wenn Bankbewegung oder markierter Satz fällt).
- Eine Zuordnung = `InvoicePayments`-Zeile + markierte Sätze unter der Bankbewegung (`ParentBooking_ID` = Bankbewegung, `DocumentNumber` =
  Rechnungsnummer, `Status='invoice_payment'`): „Bank an Debitoren“ (`liquid_first`) + „Wartekonto an Erlöskonto“ (`transfer`), ohne
  Forderung „Erlöskonto an Bank“ (`purpose_first`). Die Forderung selbst ist `transfer`.
- Eindeutiger Index `InvoicePayments(InvoiceID, BookingID)` (`IntegrityError` = „bereits verknüpft“); fehlt er wegen Dubletten, startet die
  App trotzdem, die Prüfliste meldet sie.
- Entfernt werden nur markierte Sätze, nie Sätze mit `SourceGroup`, nie unmarkierte (Altbestand, WISO).
- Prüfung, Schreiben, Neuberechnung in einer `BEGIN IMMEDIATE`-Transaktion; Konten, die `resolve_revenue_coa` anlegen könnte, vorher in
  eigenen Verbindungen auflösen – nie innerhalb.
- Bausteine für beide Wege und den Umzug auf eine Ersatzrechnung: `_assign_in_tx(tx, …)` (committet nie; `allow_cancelled` öffnet eine
  stornierte Rechnung mit positivem Betrag, nie eine Stornorechnung, nur für den Rest beim Umzug) und `_unassign(conn, …)`.
- Nur Zahlungseingänge (Betrag > 0) auf versendete Rechnungen (`PAYABLE_STATUSES`), Bankkonto mit SKR-Nummer.
- Freier Rest eines Eingangs: `Betrag − MAX(Zuordnungen, bankwirksame Sätze)` (`_free_minor_sql`, überall dieselbe Formel; Spiegel roh
  gerechnet, die Split-Summe dagegen über `bank_effect` – bewusst nicht dasselbe).
- **Freier Rest eines Ausgangs rechnet über `bank_effect`** (`_outgoing_free_minor`): `|Betrag| − MAX(−Σ Zuordnungen, −Σ Wirkung der Sätze auf
  das Konto der Bewegung)`; ein Import-Spiegel (`mirror_coa`) unter dem Ausgang verbraucht ihn. Wenige gesammelte Abfragen
  (`get_unallocated_outgoing_bookings` linear).
- Zuordnungsbetrag auf Cent gerundet (halb auf) vor dem Kappen; Steuer eines Teils anteilig (Cent), der schließende Teil bekommt Rest der
  Rechnungssteuer minus Steuer der anderen Zuordnungen (markierte Sätze: die gebuchte, sonst die anteilige).
- Status/Rest: `next_payment_status` (nie zurück auf „abgeschlossen“, „storniert“ bleibt; Rest unter einem Cent = beglichen: `money.is_covered`).

### Erlöskonto

- Aus der Rechnung (`resolve_revenue_coa`); gelernt NUR aus markierten Sätzen mit positivem Betrag (Erstattungen lehren nichts), nur in der
  Form der App (Umbuchung „Wartekonto an Erlöskonto“: Gegenkonto; Einzelsatz „Erlöskonto an Bank“: `COA`; der jüngste gewinnt). Im
  Split-Editor umgeformte Sätze, das Konto der Bankzeile (Backfill) und WISO-Sätze lehren nichts.
- Gilt nur als Erlöskonto 4000–4999 ohne Wartekonten, sonst 4400/4300; § 19: 4185 bzw. 4184 (siehe unten).
- Ist für den Satz (z. B. 5 %, 16 %) keines hinterlegt, wählt der Nutzer (`revenue_coa_id`; in der DB-Schicht geprüft: Erlöskonto, sonst
  Ablehnung). `suggest_revenue_coa` ist nur Vorauswahl.

## Forderung

- **Die Forderung (`10000 an Wartekonto`) ist nie Kind einer Bankbewegung:** `matching.receivable_condition` (10000 im SOLL – `COA` = 10000
  bei Betrag > 0 oder Gegenkonto = 10000 bei Betrag < 0; Gutschrift: `COA` = 10000, negativer Betrag, Beleg-Nr. einer Rechnung mit negativem
  Brutto – und keine Seite ein Zahlungskonto) schließt sie aus Auto-Abgleich und Editor-Kandidaten aus; `delete_transaction` hängt sie ab statt
  zu löschen. „… an 10000“ (Erlösminderung, Skonto, Forderungsverlust) ist keine Forderung und geht mit seiner Bewegung. Alle Stellen suchen
  die Forderung über diese Bedingung (auch `assign_invoice_payment`, `get_receivable_booking`).
- Buchungsliste: Forderung (eigenständig) trägt graues Abzeichen „Forderung“ statt Haken, ungefärbten Betrag (`Database.receivable_entry_ids`,
  EINE Abfrage je Seite, nie für Sätze unter einer Bankbewegung). Gruppe „Split n×“: Sind ALLE Mitglieder Forderungen, trägt die Kopfzeile das
  Abzeichen (kein `row-ok`, ungefärbter Betrag); gemischte Gruppe behält ihre Kopfzeile, nur Forderungs-Kindzeilen tragen es.
  `tests/test_receivable_row.py`
- **Stufe 7 des Auto-Abgleichs** setzt die Forderung erst auf `resolved`, wenn die Summe der Sätze „… an 10000“ gleicher Beleg-Nr. die Summe
  ALLER eigenständigen Forderungszeilen der Beleg-Nr. erreicht (`COA` = Debitoren, ohne Bankbewegung, auch schon `resolved`).
  - Forderung ist nur, was `receivable_condition` nennt UND Debitoren als `COA` führt: nicht „10000 an Kasse“, nicht „10000 an X“ mit
    negativem Betrag ohne Gutschrift-Rechnung; Gutschrift und Gegenbuchung einer Stornorechnung bleiben drin. Debitoren = ALLE Konten mit
    Nummer 10000.
  - Dieselbe Auswahl an beiden Stellen: `_after_payment_removed` über `_standalone_receivable_rows`; Stufe 7 liest alle Nummern in EINER Abfrage.
  - Die Zeilen werden gemeinsam `resolved`, nie eine für sich; `unassign` setzt sie gemeinsam zurück, sobald die Summe nicht mehr gedeckt ist.
    `tests/test_receivable_resolution.py`
  - **Robust lesen:** Beträge einzeln und roh, in Python summiert (`matching.receivables_covered`: ganze Zahlen – auch `1190000.0` – und NULL
    zählen; Text, Blob, Bruchteil, Wahrheitswert: keine Antwort, kein Statuswechsel, kein Fehler; keine SQL-Summe wegen Overflow). Beleg-Nr.
    als BLOB nach Bytes gruppiert. Die Verbindung schließt auch bei Fehler (`link_bank_to_entries` ist die Hülle um `_link_bank_to_entries_in`).
- Die Forderung geht mit der letzten Zahlung, wenn der Rechnungsstatus kein Forderungsstatus ist (`RECEIVABLE_STATUSES`).
- **Entfernt wird nur die Forderung, die die App selbst gebucht hat** (`_receivable_removal`: ohne `SourceGroup`, Wartekonto als Gegenkonto,
  keiner Bankbewegung zugeordnet), nie eine importierte oder von Hand gebuchte.
- **Ausnahme Storno-Paar:** Forderung der Ursprungsrechnung und Gegenbuchung der Stornorechnung bleiben in jedem Fall stehen
  (`_drop_receivable`, auch `remove_receivable_booking`).
- Negative Zuordnungen kennen nur der WISO-Import (`link_invoice_to_transaction`, ohne Sätze) und die Erstattung (mit Sätzen).

## Prüfliste (`find_payment_inconsistencies`)

- Nur lesend, linear; Seite *Daten*: nur auf Klick (`?check_payments=1`), nie beim Laden. Zuordnungen auf Nicht-Bankbewegungen oder mit
  `SourceGroup`-Sätzen (WISO) lösen `no_settlement`/`transfer_mismatch` nicht aus.
- Storno-Paar (nur Forderungen der App, `_app_receivable_ids`; Import löst keinen Fehlalarm aus): `storno_counter_mismatch` (von Forderung und
  Gegenbuchung nur eine), `legacy_cancelled_open` (von Hand storniert, keine Stornorechnung, Forderung steht). Intaktes Paar, auch mit
  Guthaben/Erstattung: kein Befund.
- `refund_exceeds_credit`: Σ Zuordnungen beider Belege < 0 (kein Weg der Oberfläche; Eingriffe daran vorbei, `link_invoice_to_transaction`
  prüft es nicht).
- **Sätze eines Belegs** (`_rate_findings`, je Art eine Abfrage für alle Belege):
  - `rate_split_mismatch`: Beleg mit Steuersatz je Position, dessen Positionen nicht zu den Summen passen (`tax_groups.mixed_groups`), dessen
    Forderungszeilen der App nicht genau die Satzgruppen buchen, oder der ganz bezahlt ist, ohne dass je Satz Betrag und Steuer umgebucht
    wären (dieselben Sätze wie `_mixed_booked`).
  - `rate_refund_mismatch`: Storno-Paar mit Kennzeichen, Stand je Satz lesbar (`booked_by_rate`), und je Satz mehr erstattet als gezahlt bzw.
    Betrag/Steuer gehen nicht auf (`tax_groups.booked_fault` am Topf `refund_pool`). Ein Befund je Paar, an der stornierten Rechnung.
  - `unflagged_rate_mix`: Beleg OHNE Kennzeichen mit verschiedenen Sätzen in den Positionen (verglichen nach `rate_pct`; Position ohne Satz,
    Kopf unter 0 und Angebot zählen nie). Genannt, nie umgedeutet.
  - Ein Beleg mit einem Satz ist nie betroffen (`tests/test_single_rate_snapshots.py`).
- Die Forderungen liest die Prüfliste einmal für alle (`_app_receivable_amounts`; dieselbe Bedingung `receivable_condition` und Einordnung
  `_is_app_receivable`) – nie je Rechnung nachschlagen (quadratisch); nur der Filter `invoice_id` liest gezielt.

## Zuordnung neu aufbauen (`db/invoice_repair.py`, `Database.rebuild_invoice_payments`)

- `POST /invoice/rebuild-payments`, JSON `{invoiceId[, execute, check, revenueCoaId]}`; Route in `_JSON_PATHS` + `_POST_ONLY_JSON_PATHS`.
  Repariert **eine** Rechnung (keine Sammelreparatur), nur nach Probelauf und mit Bestätigung. Schritte in dieser Reihenfolge:
  - (A) doppelte Zuordnungen desselben Paares mit gleichem Betrag zusammenfassen (kleinste ID bleibt; verschiedene Beträge nur melden).
  - (B) die als Kind an einer Bankbewegung hängende Forderung der App (genau eine, `_is_app_receivable`) lösen (`ParentBooking_ID` leer,
    `resolved` zurück – wie `remove_split_row`).
  - (C) jede Zuordnung ohne eigene Sätze neu schreiben (`_unassign` + `_assign_in_tx`) **in zwei Phasen in EINEM Savepoint:** erst alle
    Kandidatinnen entfernen, dann in der Reihenfolge der Bankbewegungen (Datum, dann ID) neu buchen; nicht angefasste Zuordnungen zählen als
    schon bezahlt. Geht eine nicht mit genau ihrem Betrag, nimmt der Savepoint den Durchgang zurück, sie bleibt samt ID liegen (`not_free`),
    die übrigen laufen noch einmal (höchstens so viele Durchgänge wie Kandidatinnen). Zahlungsdatum der neuen = Datum der Bankbewegung;
    Notiz der alten bleibt.
  - (D) offenen Betrag neu rechnen; **der Status bleibt** – nur nach A rechnet `next_payment_status` ihn mit `today` neu.
- **Kein Buchungssatz wird gelöscht oder geändert** (Ausnahme B); neue Sätze nur dazu, mit Datum/Steuerdatum der Bankbewegung und Marke.
  `_booking_snapshot_unchanged` prüft es (vorhandene Sätze der Rechnung, ihrer Bewegungen und deren Kinder Spalte für Spalte gleich) – sonst
  alles zurück, 409 `REBUILD_UNSAFE_MESSAGE`.
- **Zweites Netz:** Nennt die Prüfliste nach dem Schreiben eine neue Kategorie (z. B. `transfer_mismatch`), geht alles zurück (409
  `REBUILD_UNSAFE_MESSAGE`); die Kategorien stehen nur im Server-Log, nie in der Antwort.
- Nur D (offener Betrag) für: Storno-Paare, Rechnungen außerhalb von versendet/überfällig/Teilzahlung/bezahlt, Rechnungen ohne positiven Betrag.
- **Beleg mit Steuersatz je Position** (`MixedTaxRates`): Grund `mixed` (vor `storno_pair`), nur D (Probelauf wie Ausführung); nie ein
  Erlöskonto gesucht oder gewählt (`revenueCoaId` gilt nie, `needs_revenue_coa` kommt nie, keine Auswahl auf der Seite). Je Satz zu
  verallgemeinern wäre eine neue Regel (`docs/aufgaben.md`).
- **Bedingungen für C („R1“), alle zugleich** (nicht lockern – lieber melden als falsch reparieren):
  - Zuordnung an einer Bankbewegung mit Zahlungseingang; Bankkonto mit SKR-Nummer; Betrag ohne Bruchteil eines Cents.
  - Unter der Bewegung kein Satz mit `SourceGroup` (Import: still übergangen) und keiner mit der Nummer dieser Rechnung (außer der in B
    gelösten Forderung); jeder andere Satz trägt die Nummer einer anderen, der Bewegung zugeordneten Rechnung.
  - Forderung der Rechnung genau eine und von der App.
  - Für den Steuersatz ist ein Erlöskonto hinterlegt oder gewählt (`resolve_revenue_coa(…, create=False)`) – **die Reparatur legt nie ein
    Konto an**, auch der Probelauf nicht; fehlt es: `needs_revenue_coa` vor dem ersten Schreiben.
  - **„Nichts an anderer Stelle“** (`_rows_elsewhere`, vor jeder Kandidatin; Grund `elsewhere`, einmal je Rechnung; A, B, D laufen weiter): kein
    Satz, der keine Bankzeile ist, die Rechnungsnummer trägt (Vergleich ohne Rand-Leerraum – `_EDGE_WHITESPACE`, alles was `str.strip()`
    entfernt – und ohne Groß-/Kleinschreibung: `LOWER(TRIM(…, _EDGE_WHITESPACE))`), nicht ihre Forderung ist und nicht unter einer ihr
    zugeordneten Bankbewegung steht (also kein eigenständiger Satz, auch nicht auf der Kasse; keiner unter fremder Bewegung; keiner mit
    `SourceGroup`); und kein Befund `orphan_payment_rows`. Sonst stünde der Erlös zweimal in den Büchern.
  - **Die vorhandenen Sätze buchen nicht mehr Erlös, als die übrigen Zuordnungen erklären** (Erlös = Sätze mit Erlöskonto auf einer Seite,
    ohne Wartekonten; „Bank an Debitoren“ nie; Betrag ohne ganze Zahl sperrt): je Rechnung (`_overbooked`, Grund `overbooked`; Erlös aller
    Sätze mit ihrer Nummer außer der Forderung nicht über der Summe ihrer bleibenden Zuordnungen) und je Bewegung (`_overbooked_movement`,
    Grund `overbooked_movement`; Erlös der Sätze unter ihr nicht über der Summe der Zuordnungen dort). A, B, D laufen weiter.
- Grenze: Ein Satz ohne Rechnungsnummer, der denselben Erlös schon bucht, wird nicht erkannt – deshalb zeigt der Probelauf die Wirkung auf
  die EÜR, und der Planhinweis bittet um Prüfung.
- Nicht Angefasstes steht mit festem Grund in `skipped` (`REBUILD_SKIP_REASONS`).
- **Probelauf und Ausführung sind derselbe Code** (`_rebuild_in`, EINE `BEGIN IMMEDIATE`-Transaktion). Der Probelauf endet mit ROLLBACK und
  hinterlässt nichts (keine Zeile, keinen `sqlite_sequence`-Zähler, keinen Replay-Log-Eintrag). Die Ausführung schreibt nur mit
  `confirmed_check` = `RebuildPlan.check` (SHA-256 über Aktionen, neue Sätze ohne IDs, EÜR-Wirkung, Status, offenen Betrag, Befunde vorher/nachher,
  Gründe, Erlöskonto und nur bei Dublettenzusammenfassung den Tag; nie `UpdatedAt`). Geänderter Bestand oder fehlende Bestätigung: 409 mit
  frischem Plan, nichts geschrieben.
- Plan: EÜR-Wirkung je Datum und Erlöskonto (netto, Umsatzsteuer), fester Hinweis auf Nachtrag eines gemeldeten Zeitraums und Bitte zu
  prüfen, dass Zahlung und Erlös nicht schon anderswo gebucht sind (keine Steuerberatung). Die Seite zeigt ihn nur bei neuen Buchungssätzen.
- Route liest ID über `_json_positive_id`, `execute` als Wahrheitswert, `check` als 64 Hexziffern (nur mit `execute`), `revenueCoaId` streng;
  unerwarteter Fehler 500 `REBUILD_FAILED`, nie Ausnahmetext. Rechnungsseite: Knopf und Plan unter der Befundliste (nur mit Befund, nicht an
  Stornorechnung; Werte nur über `textContent`). `tests/test_invoice_payment_repair.py`

## Kleinunternehmer-Erlöse

- Erlöskonto eines Belegs ohne Steuerausweis (Kopfsatz −1, § 19) hängt am Datum der **Bankbewegung** der Zahlung/Erstattung, nicht am
  Rechnungsdatum: bis 31.12.2025 **4185**, ab 1.1.2026 **4184** (`small_business_revenue_number`, `SMALL_BUSINESS_SWITCH`).
- Nur ein ISO-Datum gilt; sonst lehnt die Zahlung vor dem Auflösen der Konten ab (`REVENUE_DATE_UNREADABLE`). Ohne Datum gilt heute
  (`_revenue_today`).
- **Wer der Regel folgt, entscheidet allein `uses_small_business_rule`:** nur der Platzhalter −1; Satz 0 mit Steuerausweis bleibt auf 4185.
- Ein gelerntes 4184/4185 im Topf „0 % und § 19“ zählt nicht als gelernt (`resolve_revenue_coa(tax_rate, create, on)`); ein anderes
  gelerntes Konto (4120) gilt in beiden Jahren.
- Zahlung: `_payment_prerequisites(on=)`/`payment_precheck(on=)`; `_assign_in_tx` lehnt mit `PAYMENT_CHANGED_MESSAGE` ab, wenn das Datum der
  Bewegung in der Transaktion zum anderen Konto gehört (Kontext `revenue_date_number`).
- **Eine abgelehnte Zuordnung legt kein Konto an:** Betrag ≤ 0, Zahlungsausgang (`INCOMING_ONLY_MESSAGE`), ausgeschöpfte Bewegung
  (`NOTHING_TO_ASSIGN_MESSAGE`) werden VOR den Vorbedingungen geprüft (diese legen 4184/4185 an, nie in einer Transaktion).
- Erstattung: mit markierten Sätzen das Konto der Zahlungen (über den Jahreswechsel gemischt: Wahl des Nutzers, `MIXED_REVENUE_MESSAGE`),
  ohne (Import) das Konto nach Erstattungsdatum.
- Umzug auf die Ersatzrechnung: `_payment_move_context` löst je Nummer ein Konto auf (`revenue_by_number`); jede Zahlung zieht auf das Konto
  ihres Datums, liegt sie nicht darauf, bleibt sie (`MOVE_SKIP_REASONS['revenue_account']`), bei unlesbarem Datum `revenue_date`. Wechselt
  der Kopf der Ersatzrechnung zwischen Vorbereitung und Transaktion vom Platzhalter auf 0 %, lehnt `CHANGED_MESSAGE` ab.
- Neu aufbauen: § 19 hat nie eine Wahl; je Zuordnung das Konto ihres Datums (`RebuildCandidate.revenue_coa`, `create=False`); fehlt es,
  bleibt sie liegen (`revenue_missing`; `revenue_date` bei unlesbarem Datum). Bedingungen von Umzug und Neuaufbau nicht gelockert.
- Frühere Zahlungen ab 2026 auf 4185 bleiben (kein Befund, keine Umbuchung). Standard-Kontenrahmen führt 4184, ältere Bestände über „fehlende
  Standardkonten“ oder bei der ersten Zahlung. Keine Auswertung hat eine Sonderregel. Demo-Schnappschüsse (`demo_2026`, `demo_2026_full`,
  `demo_db_2026`) nicht ändern, Code anpassen. `tests/test_small_business_revenue_4184.py`

## Skonto bei der Zahlungszuordnung

- Zahlt der Kunde bis auf höchstens den vorgesehenen Abzug, bucht die Zuordnung den Rest auf Wunsch als Skonto aus
  (`assign_invoice_payment(…, skonto=True)`) – **nur auf Klick, nie vorausgewählt**.
- `InvoicePayments.SkontoAmount` (Minor Units, NULL = keines; nur eine positive ganze Zahl zählt). `Amount` bleibt das gezahlte Geld (freier
  Rest, Storno-Guthaben, Erstattung rechnen damit); erledigt ist `Amount` plus Skonto (`settled_sql`, die EINE Stelle: `_recalc_invoice`,
  `_assign_in_tx`, Prüfliste, Schritt D des Neuaufbaus).
- **Sätze** (markiert, in dieser Reihenfolge; Beispiel 119,00 / gezahlt 116,62): „Bank an Debitoren“ 119,00 (nur mit Forderung),
  „Wartekonto an Erlöskonto“ bzw. ohne Forderung „Erlöskonto an Bank“ 119,00 mit Rest der Rechnungssteuer, zuletzt **„Skonto-Konto an Bank“**
  (`purpose_first`) −2,38 mit −0,38 Steuer. Nie „Skonto an Debitoren“ (EÜR ohne Steuer, Summen- und Saldenliste unzerlegt). DATEV: zwei Bankzeilen.
- **Konten** (`SKONTO_ACCOUNTS`): 19 % → 4736, 7 % → 4731, 0 %/§ 19 → 4730 ohne Steuer (Standardkonten; fehlt eines, legt die erste Zuordnung
  es vor der Transaktion an – eine abgelehnte nichts). Ein Skonto-Konto lehrt nie ein Erlöskonto, der Skonto-Satz zählt nicht in
  `_allocations_tax` und steht nicht in `payment_revenue_choices` und der Wahl eines Stammdaten-Steuersatzes.
- **Ein Skonto-Satz wird nie allein am Konto erkannt – maßgeblich ist `SkontoAmount`:** Ein Skonto-Konto kann auch Erlöskonto einer Zahlung
  sein; sein Erlössatz zählt dann in `_allocations_tax` und löst weder `skonto_mismatch` noch `SKONTO_STORNO_EDITED` aus. Ohne Wert in der
  Spalte erkennen Stornieren und `_skonto_booked` Skonto nur an einem Satz mit negativem Betrag. `tests/test_skonto_revenue_account.py`
- **Regeln** (`skonto_terms`, `_skonto_state` – EINE Stelle für Vorprüfung und Transaktion): Skonto = Rest nach der Zahlung in ganzen Cent,
  höchstens `skonto_amount(Brutto, Prozent)`; Frist gezeigt, nie geprüft; je Rechnung höchstens EIN Skonto; Steuer = Skonto × Satz / (100 + Satz),
  EINMAL auf den Cent gerundet (`skonto_tax`). Nur Rechnungen mit einem Satz (19 %, 7 %, 0 %, § 19); Beleg mit Steuersatz je Position,
  Stammdaten-Satz oder anderem Kopfsatz lehnt mit festem Satz ab (`SKONTO_*`).
- **Entfernen:** Die Zahlung mit Skonto geht auf jedem Weg samt Skonto-Sätzen; solange sie hängt, lässt sich keine ANDERE Zahlung der
  Rechnung entfernen (`SKONTO_REMOVAL_REFUSED`, in der einen Prüfung aller Entfernen-Wege, `_refund_removal_blocker`).
- **Stornieren nimmt das Skonto zurück** (`_reverse_skonto_in`, in der Transaktion von `cancel_invoice`): drei markierte Sätze unter der
  Bewegung (ohne Forderung zwei), Beleg-Nr. der stornierten Rechnung, Datum = das spätere von Stornodatum und Bankbewegung, danach
  `SkontoAmount` NULL. Im Editor geänderte Skonto-Sätze: Ablehnung `SKONTO_STORNO_EDITED`. Folge: SuSa nennt `child_other_period` für die Monate
  zwischen Zahlung und Storno.
- Umzug: Zahlung mit Skonto zieht nicht um (`MOVE_SKIP_REASONS['skonto']`); Neuaufbau rechnet nur den offenen Betrag (Grund `skonto`).
- Prüfliste `skonto_mismatch` (Sätze passen nicht zu `SkontoAmount` oder ungültiger Wert). Vorschlagssignal „Betrag nach Skonto stimmt“ (freier
  Betrag = offen − Skonto, 50 Punkte; braucht Namenswort oder Nummer).
- Oberfläche: Rechnungsseite (Häkchen „Rest … als Skonto ausbuchen“ je Kandidat, nur sichtbar, wenn der Rest passt), Buchungsmaske (Feld
  `skonto=1`, Vorprüfung vor dem Schreiben), Kasten der Zahlungseingänge („Mit Skonto zuordnen“). `POST /invoice/link-payment` liest `skonto`
  streng als Wahrheitswert; an einer Stornorechnung 400 (`SKONTO_REFUND`). Keine Steuerberatung in Texten. `tests/test_skonto_*.py`

## Zahlungsvorschläge (`db/payment_matching.py` rein, `db/payment_suggestions.py` nur lesend)

- Zu einem Zahlungseingang wird die passende offene Rechnung **vorgeschlagen – zugeordnet wird allein auf Klick** über die bestehenden Wege
  (`POST /invoice/link-payment`, Feld `invoice_id` der Maske → `assign_invoice_payment`), nichts vorausgewählt; **nicht einführen, dass ein
  Speichern still zuordnet.**
- Vorschlag = Rechnungsnummer im Verwendungszweck oder Empfänger ODER (freier Betrag = offener Betrag UND gemeinsames Namenswort von Empfänger
  und Kunde). Gleicher Betrag allein nie.
- **Es zählt nur, was von der Bank kommt:** Beleg-Nr. und Kontakt der Bankzeile zählen bewusst nicht (eine Zuordnung trägt beides aus der Rechnung
  ein, `_backfill_booking_skr`, und es bleibt nach dem Entfernen stehen – sonst wäre eine falsch zugeordnete, gelöste Rechnung der stärkste Vorschlag).
- Kundenname = Firma, sonst Name (nie der Ansprechpartner). Nur Euro (leer oder `EUR`, beide Seiten). Punkte 100/50/30, feste Begründungen
  (`reason_text`); Zahlung mehr als 7 Tage vor Rechnungsdatum nur mit Nummer; höchstens 5 je Bewegung bzw. Rechnung.
- **Nummer im Text** (Originaltext, ohne Rücksicht auf Schreibweise): An einer Fuge mit einem Buchstaben EIN Trennzeichen (Leerraum, `-`, `_`,
  `/`, `.`: `R-26003`); zwischen zwei Ziffern nur, wo die Nummer selbst eines trägt (`_number_parts`; `26001` nicht in `26.001,00`, `R26003` nicht
  in `R26 003`), ein Punkt nur, wo die Nummer selbst einen trägt; Ziffernsperre an dem Rand, an dem die Nummer mit einer Ziffer beginnt/endet
  (`R26001` nicht in `R260010`). Brauchbar: mindestens 4 Zeichen mit einer Ziffer, rein numerisch ab 5; höchstens 40 Zeichen; Felder auf 2000
  Zeichen gekappt; linear. **Nicht lockern – lieber ein Vorschlag weniger als ein falscher.**
- **`pair_up`** (Eins-zu-eins-Liste): Paare nach (−Punkte, Datumsabstand, IDs), jede Bewegung/Rechnung höchstens einmal; `ambiguous` bei einem
  weiteren Paar gleicher Punktzahl; ein Paar in `InvoicePayments` ist nie Vorschlag.
- **Bewegungen:** nur Bankbewegungen mit Betrag > 0, ohne Kind, ohne Zuordnung (frei = Betrag). `_UNBOOKED_MOVEMENTS` liest die Zuordnungen EINMAL
  über `NOT IN` (kein Index auf `BookingID`; keine Unterabfrage je Bewegung; ein Test zählt SQLite-Schritte). Der freie Rest nach
  `_free_minor_sql` gilt für die EINE Bewegung der Maske. Kaputte Werte (Text-Betrag, unlesbares Datum) sind kein Kandidat, nie ein Fehler.
- **Seiten:** Kasten `#paySuggest` über der Buchungsliste (alle Zeiträume, höchstens 20 Zeilen, `payAssign` lädt danach ohne `err` neu,
  `refreshTable()` erneuert ihn nach Import; nicht im Bereich, den `openEditForm` austauscht); Maske einer Bankbewegung mit freiem Rest
  (Vorschläge zuerst in „Rechnung“, „Übernehmen“ setzt nur den Wert); Rechnungsseite (Vorschläge zuerst). Scheitert eine Abfrage, bleibt die
  Seite stehen (leer, Fehler im Server-Log).
- **Eindeutig** (`payment_matching.is_certain`, rein): Die Rechnungsnummer steht im Verwendungszweck UND der Betrag stimmt (`REASON_NUMBER` und `REASON_AMOUNT`), das Paar ist nicht `ambiguous`
  und kein `skonto` – nichts anderes. Betrag und Kunde ohne Nummer, die Nummer mit abweichendem Betrag und der Betrag nach Skonto bleiben Vorschläge mit Einzelklick.
  - **„Alle eindeutigen zuordnen (n)“** im Kasten, nur mit mindestens zwei eindeutigen Zeilen im Kasten (n = deren Zahl; dazu der Satz „Eindeutig: …“ im Hinweis). Jede Zeile trägt `data-invoice`/`data-booking`
    (Zahlen), eine eindeutige `data-certain="1"`. `payAssignAll` fragt über `appConfirm` und ordnet dann NACHEINANDER je Zeile über den bestehenden Weg `POST /invoice/link-payment` zu
    (`{invoice_id, transaction_id}`, nie `skonto`, keine eigene Route); solange es läuft, sind alle Knöpfe des Kastens gesperrt.
  - Alles gelungen: neu laden wie `payAssign`. Sonst verschwinden die gelungenen Zeilen, die Knöpfe sind frei (der Sammelknopf zählt die übrigen Zeilen neu und entfällt unter zwei), und `appMsg` nennt „<a> zugeordnet, <b> nicht: <erste Meldung>“ (nur als Text).
- **Abzeichen „Rechnung?“** an einer offenen Bankbewegung ohne Kind in der Buchungsliste, zu der die Übersicht einen Vorschlag hat (Titel „Passt zur offenen Rechnung <Nr>“, Nummer maskiert): nur ein Hinweis, auch nach
  `refreshTable()`. Die Übersicht wird EINMAL je Seite gelesen (`PAY_BADGE_LIMIT` = 500 Paare; der Kasten zeigt davon die ersten `PAY_SUGGEST_LIMIT`). Scheitert die Abfrage: kein Kasten, keine Abzeichen, die Seite steht (Fehler im Server-Log).
- **Fehlerweg der Maske:** Scheitert die Zuordnung nach dem Speichern, bleibt der Nutzer bei der Buchung (303 auf
  `/transactions/edit?id=<ID>&err=…`, `PAYMENT_NOT_ASSIGNED`).
- Bekannte Grenzen: nur voller Skonto-Rest, andere Teilbeträge nicht; Umbruch der Bank mitten in der Nummer ist kein Treffer; Rechnung mit
  nötiger Erlöskonto-Wahl lässt sich im Kasten nicht zuordnen; vorgeschlagen wird auch eine Bewegung, deren Bankkonto kein Konto im
  Kontenrahmen hat (der Klick meldet es).
- Werte im Skript des Buchungssatz-Editors (`SPLIT_BANK_TEXT`, `SPLIT_BANK_DOCNR`) nur als JSON (`script_json`; Zeilenumbrüche im
  Verwendungszweck). `tests/test_split_editor_script_values.py`
- Tests: `tests/test_payment_matching.py`, `tests/test_payment_suggestions.py`
