# Echte Firebird-Dateien: Satzversionen nicht abgeschlossener Vorgänge

`firebird3_open_tx_4k.fdb.gz` (Seitengröße 4096) und `firebird3_open_tx_8k.fdb.gz` (8192) sind **echte
Firebird-3.0-Dateien** (ODS 12, von Firebird selbst geschrieben), keine mit `tests/fdb_builder.py` gebauten.
Sie zeigen, wie eine WISO-Datenbank aussieht, wenn man sie kopiert, **während WISO geöffnet ist** oder
nicht sauber beendet wurde: Sätze tragen Versionen einer Transaktion, die nie abgeschlossen wurde.

So sind sie entstanden (die `.sql`-Dateien sind die Dokumentation, ohne Firebird nicht ausführbar):

1. `*.create.sql` – Datenbank anlegen, Tabelle `T` (`ID`, `TXT`, `AMOUNT`, in der 4k-Datei dazu `WIDE VARCHAR(12000)` und
   `MEMO BLOB`), Zeilen einfügen und mit `COMMIT` abschließen (in der 4k-Datei danach noch eine abgeschlossene Änderung).
2. `*.open.sql` – in einer **zweiten Verbindung** Änderungen, Löschungen und neue Zeilen, ohne `COMMIT`. Diese Verbindung
   blieb offen, während die Datei kopiert wurde.

Der Inhalt ist erfunden (Texte wie „eins unveraendert“, Zufallszeichen in `WIDE`); es stehen keine echten Daten darin.

`expected.json` nennt je Datei den Inhalt von `T` in zwei Lesarten: `committed` ist der abgeschlossene Stand – was der Leser
liefern muss –, `as_stored_today` das, was ein Leser liefert, der einfach die oberste Version jedes Satzes nimmt.
`WIDE` steht dort als Länge und SHA-256. In beiden Dateien ist die offene Transaktion die Nummer 9; die 4k-Datei
enthält 8 Sätze aus nicht abgeschlossenen Vorgängen, die 8k-Datei 3.

Die Tests (`tests/test_wiso_fdb_committed_versions.py`) entpacken die Dateien nach `tmp_path`.
