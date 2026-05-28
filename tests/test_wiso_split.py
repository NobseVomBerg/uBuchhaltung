"""
Tests für WISO-Split-Buchungen: Empfänger aus dem Tabellen-Export wird auf
ALLE Teilbuchungen einer Split-Gruppe übertragen.

Zwei Split-Varianten:
- BookingGroup-Split (Original-Import erzeugt mehrere Entry-Zeilen mit gleicher
  Belegnummer und BookingGroup_ID; kein Bank-Parent).
- Bank-Parent-Split (Bank-Buchung + verknüpfte Entry-Kinder via ParentBooking_ID).

Inhalte sind zufällig/anonymisiert; Assertions vergleichen gegen die erzeugten Werte.
"""
import random
import uuid


def _rand_name():
    return f"Firma {uuid.uuid4().hex[:6].upper()}"


def _rand_doc():
    return f"25F{random.randint(100, 999)}"


def _distinct_amounts(n):
    """n verschiedene Beträge (vermeidet die Duplikat-Erkennung im Import)."""
    return [c / 100 for c in random.sample(range(500, 5000), n)]


def _orig_split_csv(doc, lines, date='03.07.2025 06:40'):
    """Bewegungsdaten-Export (9 Spalten). lines: [(konto, amount, schluessel), ...]."""
    header = "ID;DATUM;KONTO;GEGENKONTO;TEXT;REFERENZNUMMER;BRUTTOBETRAG;SCHLUESSEL;USTIDENTNUMMER"
    rows = [header]
    for idx, (konto, amount, schl) in enumerate(lines, start=1):
        rows.append(f'{idx};{date};{konto};1460;"Erfahrungsaustausch";"{doc}";{amount:.2f};{schl};')
    return ("\n".join(rows) + "\n").encode('utf-8')


def _table_split_csv(doc, recipient, total, date='03.07.2025'):
    """Tabellen-Export mit einer Splittbuchung-Zeile (Gesamtbetrag, deutsches Dezimal)."""
    header = "Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag"
    betrag = f"{-total:.2f}".replace('.', ',')
    row = f"{date};{recipient};;Erfahrungsaustausch;Splittbuchung;{doc};{betrag}"
    return (header + "\n" + row + "\n").encode('utf-8')


def _bookings_by_doc(db, doc):
    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute(
        'SELECT ID, RecipientClient, TaxRate, BookingGroup_ID, Text '
        'FROM Bookings WHERE DocumentNumber=? ORDER BY ID', (doc,))
    rows = cur.fetchall()
    conn.close()
    return rows


def test_recipient_propagated_to_bookinggroup_split(db_with_coa):
    db = db_with_coa
    doc = _rand_doc()
    recipient = _rand_name()
    amts = _distinct_amounts(4)
    total = round(sum(amts), 2)
    schluessel = ['490', '490', '490', '']  # 3× ohne Vorsteuerabzug, 1× ohne Schlüssel
    lines = [(6815, amts[i], schluessel[i]) for i in range(4)]

    res1 = db.import_wiso_csv(_orig_split_csv(doc, lines))
    assert res1['format'] == 'original'
    assert res1['imported'] == 4, res1

    rows = _bookings_by_doc(db, doc)
    assert len(rows) == 4
    # BookingGroup-Split: alle Zeilen in einer Gruppe
    assert all(r[3] is not None for r in rows)
    # Steuersatz: 490 -> 0.0, ohne Schlüssel -> None
    tax_rates = sorted([(r[2] if r[2] is not None else -1) for r in rows])
    assert tax_rates == [-1, 0.0, 0.0, 0.0]
    # Vor dem Tabellen-Import: noch kein Empfänger
    assert all((r[1] or '') == '' for r in rows)

    res2 = db.import_wiso_csv(_table_split_csv(doc, recipient, total))
    assert res2['format'] == 'table'
    assert res2['updated'] >= 1, res2

    rows2 = _bookings_by_doc(db, doc)
    assert len(rows2) == 4
    # Empfänger auf ALLEN Teilbuchungen
    assert all(r[1] == recipient for r in rows2), rows2
    # Zeilen-Text bleibt erhalten (nicht überschrieben)
    assert all(r[4] == 'Erfahrungsaustausch' for r in rows2)


def test_recipient_propagated_to_bank_parent_split(db_with_coa):
    db = db_with_coa
    doc = _rand_doc()
    recipient = _rand_name()
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}

    parent = db.insert_booking(date_booking='2025-07-03', amount=-100.0, booking_type='bank')
    c1 = db.insert_booking(date_booking='2025-07-03', amount=-60.0, coa_id=coa.get(6815),
                           text='Pos1', document_number=doc, booking_type='entry',
                           parent_booking_id=parent)
    c2 = db.insert_booking(date_booking='2025-07-03', amount=-40.0, coa_id=coa.get(4400),
                           text='Pos2', document_number=doc, booking_type='entry',
                           parent_booking_id=parent)

    res = db.import_wiso_csv(_table_split_csv(doc, recipient, 100.0))
    assert res['format'] == 'table'
    assert res['updated'] >= 1, res

    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT ID, RecipientClient FROM Bookings WHERE ID IN (?,?,?)', (parent, c1, c2))
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 3
    assert all(r[1] == recipient for r in rows), rows


def test_split_without_document_number(db_with_coa):
    """Split-Buchungen ohne Belegnummer aus Tabellen-Export werden zugeordnet.

    Problem: WISO-Tabellen-Export hat oft keine Belegnummer (z.B. bei 1%-Methode/
    Privatnutzung), aber die Bewegungsdaten-Zeilen (Split-Teile) haben unterschiedliche
    Beträge. Der Tabellen-Export kombiniert sie zur Summe. Der Import muss
    BookingGroup-Splits erkennen, auch ohne Belegnummer.
    """
    db = db_with_coa
    recipient = _rand_name()
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}

    # Zwei Split-Buchungen ohne Belegnummer (wie bei 1%-Methode/Privatnutzung)
    # Alle Werte frei erfunden: Teilbeträge 150,00 + 50,00 = 200,00
    c1 = db.insert_booking(date_booking='2025-12-31', amount=-150.00, coa_id=coa.get(4645),
                           text='E-Firmenwagen Privatnutzung')
    c2 = db.insert_booking(date_booking='2025-12-31', amount=-50.00, coa_id=coa.get(4639),
                           text='E-Firmenwagen Privatnutzung')

    # Tabellen-Export ohne Belegnummer, mit Summe 200,00
    header = "Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag"
    row = f"31.12.2025;{recipient};;E-Firmenwagen Privatnutzung;Splittbuchung;;-200,00"
    text = (header + "\n" + row + "\n").encode('utf-8')

    res = db.import_wiso_csv(text)
    assert res['format'] == 'table'
    assert res['updated'] >= 1, res

    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT ID, RecipientClient FROM Bookings WHERE ID IN (?,?)', (c1, c2))
    rows = cur.fetchall()
    conn.close()
    assert len(rows) == 2
    # Empfänger auf BEIDEN Teilbuchungen
    assert all(r[1] == recipient for r in rows), rows


def test_multiple_splits_same_day_separated_by_text(db_with_coa):
    """Mehrere unabhängige Split-Gruppen am selben Datum ohne Belegnummer.

    Kritischer Fall: Bei der 1%-Methode (Privatnutzung) entstehen mehrere
    Split-Gruppen pro Tag ohne Belegnummern (z.B. E-Fahrzeug vs. PKW).
    Die Zuordnung muss über den Verwendungszweck-Text erfolgen.
    Zeilenumbrüche müssen normalisiert werden.

    Beispiel (alle Werte frei erfunden):
    - E-Firmenwagen (0,25%-Methode): 150,00 + 50,00 = 200,00
    - Firmen-PKW (1%-Methode): 300,00 + 100,00 = 400,00

    Der Tabellen-Export hat zwei Zeilen, eine für jede Split-Gruppe.
    Jede Zeile muss der richtigen Gruppe zugeordnet werden, nicht beide
    zu einer kombiniert.
    """
    db = db_with_coa
    recipient = _rand_name()
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}

    # Split-Gruppe 1: E-Firmenwagen (zwei Zeilen)
    e1 = db.insert_booking(date_booking='2025-12-31', amount=-150.00, coa_id=coa.get(4645),
                           text='E-Firmenwagen Privatnutzung')
    e2 = db.insert_booking(date_booking='2025-12-31', amount=-50.00, coa_id=coa.get(4639),
                           text='E-Firmenwagen Privatnutzung')

    # Split-Gruppe 2: Firmen-PKW (zwei Zeilen) – anderer Text!
    im1 = db.insert_booking(date_booking='2025-12-31', amount=-300.00, coa_id=coa.get(4650),
                            text='Firmen-PKW Privatnutzung')
    im2 = db.insert_booking(date_booking='2025-12-31', amount=-100.00, coa_id=coa.get(4659),
                            text='Firmen-PKW Privatnutzung')

    # Tabellen-Export: zwei Zeilen, eine für jede Gruppe
    # E-Firmenwagen mit Zeilenumbruch im Text (wird normalisiert)
    # CSV-Format: Zeilenumbrüche müssen in Anführungszeichen stehen
    # WICHTIG: WISO-Export nutzt CP1252-Encoding, nicht UTF-8!
    header = "Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag"
    row1 = f'31.12.2025;{recipient};;"E-Firmenwagen\nPrivatnutzung";Splittbuchung;;-200,00'
    row2 = f"31.12.2025;{recipient};;Firmen-PKW Privatnutzung;Splittbuchung;;-400,00"
    text = (header + "\n" + row1 + "\n" + row2 + "\n").encode('cp1252')

    res = db.import_wiso_csv(text)
    assert res['format'] == 'table'
    # Zwei Zeilen sollten zu zwei Updates führen
    assert res['updated'] == 2, f"Expected 2 updates, got {res['updated']}"

    conn = db._get_connection()
    cur = conn.cursor()

    # Prüfe Elektroauto-Gruppe
    cur.execute('SELECT RecipientClient FROM Bookings WHERE ID IN (?,?)', (e1, e2))
    e_rows = cur.fetchall()
    assert len(e_rows) == 2
    assert all(r[0] == recipient for r in e_rows), f"Elektroauto rows: {e_rows}"

    # Prüfe Immobilien-Gruppe
    cur.execute('SELECT RecipientClient FROM Bookings WHERE ID IN (?,?)', (im1, im2))
    im_rows = cur.fetchall()
    assert len(im_rows) == 2
    assert all(r[0] == recipient for r in im_rows), f"Immobilie rows: {im_rows}"

    conn.close()


def test_bank_entry_pair_without_doc_number_matched(db_with_coa):
    """bank+entry-Paar ohne Belegnummer wird korrekt als Treffer erkannt.

    Realfall: Privatentnahme, Bankgebühr oder Zinszahlung ohne Belegnummer.
    Die bank-Buchung (DocNr=NULL) und ihr entry-Child (DocNr='',
    ParentBooking_ID=bank.ID) haben identisches Datum, Betrag und Text.
    Stage 1 findet beide → len==2; Stage A muss sie als Paar erkennen und
    RecipientClient + IBAN auf BEIDEN aktualisieren, ohne dass sie in
    not_found landen.
    """
    db = db_with_coa
    recipient = _rand_name()
    iban = "DE00 0000 0000 0000 0000 00"

    bank_id = db.insert_booking(
        date_booking='2025-09-01', amount=-1000.00,
        booking_type='bank', text='Privatentnahme September'
    )
    entry_id = db.insert_booking(
        date_booking='2025-09-01', amount=-1000.00,
        booking_type='entry', text='Privatentnahme September',
        parent_booking_id=bank_id
    )

    header = "Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag"
    row = f"01.09.2025;{recipient};{iban};Privatentnahme September;;;-1000,00"
    text = (header + "\n" + row + "\n").encode('utf-8')

    res = db.import_wiso_csv(text)
    assert res['format'] == 'table'
    assert res['not_found'] == [], f"bank+entry-Paar nicht gefunden: {res['not_found']}"
    assert res['updated'] >= 1, res

    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT ID, RecipientClient FROM Bookings WHERE ID IN (?,?)', (bank_id, entry_id))
    rows = dict(cur.fetchall())
    conn.close()
    assert rows[bank_id] == recipient, f"Bank-Buchung nicht aktualisiert: {rows}"
    assert rows[entry_id] == recipient, f"Entry-Buchung nicht aktualisiert: {rows}"


def test_similar_individual_bookings_disambiguated_by_text(db_with_coa):
    """Zwei gleichartige Einzelbuchungen (KEIN Split) mit identischem Datum
    und Betrag, die sich nur im Verwendungszweck unterscheiden.

    Beispiel: zwei Privatentnahmen am selben Tag, gleicher Betrag, keine
    Belegnummer. Der Verwendungszweck unterscheidet sich nur in der
    EREF/MREF-Referenz. Über Datum+Betrag allein sind sie mehrdeutig –
    die Zuordnung muss über den Text erfolgen.

    Jede Tabellen-Zeile darf nur GENAU EINE Buchung treffen (Gruppensumme =
    Betrag), nicht beide kombiniert (sonst Summe = 2×Betrag → kein Match).

    Hinweis: alle Werte frei erfunden (Muster-/Testdaten, z.B. 123-456789-0123).
    """
    db = db_with_coa
    recipient = _rand_name()
    iban = "DE00 1234 5678 9012 3456 78"
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}

    # Verwendungszweck mit gemeinsamem Rahmen; nur die Referenz unterscheidet sich.
    def _oneline(ref):
        # Einzeilig, wie aus dem Original-Import gespeichert.
        return (f"123-456789-0123 TESTSHOP op pp {ref} EREF "
                f": {ref} MREF: zz 0a1b2c3d4e5f6g7h)00 "
                f"0 CRED: DE00ZZZ00000000000 IBAN: DE00123456789012345678 0 BIC: MUSTDEFF")

    def _multiline(ref):
        # Wie im Tabellen-Export: gleiche Daten, aber mit Zeilenumbrüchen.
        return (f"123-456789-0123 TESTSHOP\nop pp {ref} EREF\n"
                f": {ref} MREF: zz\n0a1b2c3d4e5f6g7h)00\n"
                f"0 CRED: DE00ZZZ00000000000\nIBAN: DE00123456789012345678\n0 BIC: MUSTDEFF\n")

    ref_a = "REF-AAAA-1111"
    ref_b = "REF-BBBB-2222"

    # Zwei Einzelbuchungen, gleiches Datum + Betrag, keine Belegnummer
    b_a = db.insert_booking(date_booking='2024-03-15', amount=123.45,
                            coa_id=coa.get(2100), text=_oneline(ref_a))
    b_b = db.insert_booking(date_booking='2024-03-15', amount=123.45,
                            coa_id=coa.get(2100), text=_oneline(ref_b))

    # Tabellen-Export: zwei Zeilen mit Zeilenumbrüchen im Verwendungszweck
    header = "Buchungsdatum;Empf./Auft.;Konto-Nr. / IBAN;Verwendungszweck;Kategorie;Beleg Nr.;Betrag"
    row1 = f'15.03.2024;{recipient};{iban};"{_multiline(ref_a)}";Privatentnahmen;;-123,45'
    row2 = f'15.03.2024;{recipient};{iban};"{_multiline(ref_b)}";Privatentnahmen;;-123,45'
    text = (header + "\n" + row1 + "\n" + row2 + "\n").encode('cp1252')

    res = db.import_wiso_csv(text)
    assert res['format'] == 'table'
    # Beide Zeilen müssen je genau eine Buchung treffen → 2 Updates, nichts offen
    assert res['updated'] == 2, f"Expected 2 updates, got {res['updated']}; not_found={res.get('not_found')}"
    assert res['not_found'] == [], res['not_found']

    conn = db._get_connection()
    cur = conn.cursor()
    cur.execute('SELECT ID, RecipientClient FROM Bookings WHERE ID IN (?,?)', (b_a, b_b))
    rows = dict(cur.fetchall())
    conn.close()
    assert rows[b_a] == recipient, rows
    assert rows[b_b] == recipient, rows
