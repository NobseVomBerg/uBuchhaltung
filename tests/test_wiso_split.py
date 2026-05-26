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

    Problem: WISO-Tabellen-Export hat oft keine Belegnummer, aber die
    Bewegungsdaten-Zeilen (Split-Teile) haben unterschiedliche Beträge.
    Der Tabellen-Export kombiniert sie zur Summe. Der Import muss
    BookingGroup-Splits erkennen, auch ohne Belegnummer.
    """
    db = db_with_coa
    recipient = _rand_name()
    coa = {r[2]: r[0] for r in db.fetch_chart_of_accounts()}

    # Zwei Split-Buchungen ohne Belegnummer (wie im AFA-Abschreibungs-Fall)
    c1 = db.insert_booking(date_booking='2025-12-31', amount=-150.00, coa_id=coa.get(4645),
                           text='E-Firmenwagen Privatnutzung')
    c2 = db.insert_booking(date_booking='2025-12-31', amount=-50.00, coa_id=coa.get(4639),
                           text='E-Firmenwagen Privatnutzung')

    # Tabellen-Export ohne Belegnummer, mit Summe 200.00
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
