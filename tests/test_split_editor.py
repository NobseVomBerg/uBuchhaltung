# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Buchungssatz-Editor der Bankbewegung: Splits anlegen, ändern, auflösen.

Die Buchungssätze einer Bankbewegung werden als gleichnamige Formularfelder
(split_id, split_amount, …) gesendet; Index = Zeile. Werte fließen nur beim
ANLEGEN einer Zeile von der Bankbewegung nach unten – danach ist jede
Teilbuchung eigenständig. Eine unvollständige Summe ist erlaubt
(Teilerfassung) und wird in der Übersicht als offener Rest ausgewiesen.
"""
from decimal import Decimal
from urllib.parse import urlencode

from server.app import parse_form
from server.handlers import handle_add_transaction
from server.pages_transactions import PageTransactions


def _through_server_parser(post_data):
    """POST wie im Server verschicken: urlencode → server.app.parse_form.

    Ohne diesen Umweg prüfte der Test nur die von Hand gebauten Listen und
    nicht, was aus dem echten Formular ankommt.
    """
    pairs = [(k, v) for k, vs in post_data.items() for v in vs]
    return parse_form(urlencode(pairs))


def _bank_account(db, name='Split-Bank', skr=1800):
    db.insert_account(name, 'Owner', 'DE00SPLIT', 'BIC', 'Bank',
                      is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _bank_booking(db, acct, amount=-238.00, **kw):
    return db.insert_booking(kw.pop('date', '2026-03-01'), amount,
                             account_id=acct, booking_type='bank', **kw)


def _post(bank_id, acct, rows, amount='-238.00', **kw):
    """rows: Liste von (id, amount, coa, rate, tax, docnr, text)."""
    data = {'transaction_id': [str(bank_id)], 'date': ['2026-03-01'],
            'amount': [amount], 'account': [str(acct)], 'currency': ['EUR'],
            'split_id': [], 'split_amount': [], 'split_coa': [],
            'split_tax_rate': [], 'split_tax_amount': [],
            'split_docnr': [], 'split_text': []}
    for rid, amt, coa, rate, tax, docnr, text in rows:
        data['split_id'].append(str(rid) if rid else '')
        data['split_amount'].append(str(amt))
        data['split_coa'].append(str(coa) if coa else '')
        data['split_tax_rate'].append(str(rate) if rate else '')
        data['split_tax_amount'].append(str(tax) if tax else '')
        data['split_docnr'].append(docnr)
        data['split_text'].append(text)
    data.update({k: [str(v)] for k, v in kw.items()})
    return data


def _children(db, bank_id):
    return {c[0]: c for c in db.get_child_bookings_for_bank(bank_id)}


def test_create_split_from_bank_booking(tmp_db):
    """Zwei Zeilen posten → zwei Buchungssätze mit Bank-Gegenkonto, EÜR
    verteilt die Beträge auf beide Konten."""
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    coa_b = tmp_db.get_coa_id_by_account_number(6300)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    bk = _bank_booking(tmp_db, acct)

    handle_add_transaction(tmp_db, _post(bk, acct, [
        ('', '-200.00', coa_a, 19, '-31.93', '26F123_A', 'Büromaterial'),
        ('', '-38.00', coa_b, 19, '-6.07', '26F123_B', 'Versand'),
    ]))

    kids = list(_children(tmp_db, bk).values())
    assert len(kids) == 2
    assert {k[1] for k in kids} == {coa_a, coa_b}
    assert all(k[2] == bank_coa for k in kids)       # Gegenkonto = Bankkonto
    assert sum(k[4] for k in kids) == Decimal('-238.0000')

    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert euer.get(6815) is not None and euer.get(6300) is not None


def test_extend_and_reduce_split(tmp_db):
    """Zeile ergänzen und wieder entfernen – die verbleibenden bleiben intakt."""
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    coa_b = tmp_db.get_coa_id_by_account_number(6300)
    bk = _bank_booking(tmp_db, acct)

    handle_add_transaction(tmp_db, _post(bk, acct, [
        ('', '-200.00', coa_a, '', '', 'A', 'Teil A'),
    ]))
    first_id = list(_children(tmp_db, bk))[0]

    # dritte Zeile ergänzen (Teilerfassung, Summe stimmt noch nicht)
    handle_add_transaction(tmp_db, _post(bk, acct, [
        (first_id, '-200.00', coa_a, '', '', 'A', 'Teil A'),
        ('', '-38.00', coa_b, '', '', 'B', 'Teil B'),
    ]))
    assert len(_children(tmp_db, bk)) == 2

    # zweite Zeile wieder entfernen → nur noch die erste
    handle_add_transaction(tmp_db, _post(bk, acct, [
        (first_id, '-238.00', coa_a, '', '', 'A', 'Teil A'),
    ]))
    kids = _children(tmp_db, bk)
    assert list(kids) == [first_id]
    assert kids[first_id][4] == Decimal('-238.0000')


def test_partial_capture_is_saved(tmp_db):
    """Unvollständige Summe wird gespeichert (kein Block) und in der Übersicht
    als "offen" ausgewiesen; nach Ergänzen erscheint der Haken."""
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    coa_b = tmp_db.get_coa_id_by_account_number(6300)
    bk = _bank_booking(tmp_db, acct)

    status, _ = handle_add_transaction(tmp_db, _post(bk, acct, [
        ('', '-200.00', coa_a, '', '', 'A', 'Teil A'),
    ]))
    assert status == 303
    assert len(_children(tmp_db, bk)) == 1

    html = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    # Badge bleibt schmal ("offen"); der genaue Rest steht nur im Tooltip,
    # sonst verdeckt er den Bearbeiten-Stift.
    assert 'offener Rest -38.00' in html
    assert '>offen</span>' in html
    assert 'Bank + Buchungssätze vollständig' not in html

    first_id = list(_children(tmp_db, bk))[0]
    handle_add_transaction(tmp_db, _post(bk, acct, [
        (first_id, '-200.00', coa_a, '', '', 'A', 'Teil A'),
        ('', '-38.00', coa_b, '', '', 'B', 'Teil B'),
    ]))
    html = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'Bank + Buchungssätze vollständig' in html
    assert 'offener Rest -38.00' not in html


def test_bank_document_number_survives_editor_save(tmp_db):
    """Die Beleg-Nr. der Bankbewegung bleibt beim Speichern erhalten und wird
    in der Übersicht gezeigt (Basis für die Suffixe der Teilbuchungen)."""
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    bk = _bank_booking(tmp_db, acct, document_number='26F200')

    handle_add_transaction(tmp_db, _post(
        bk, acct, [('', '-238.00', coa_a, '', '', '26F200_A', 'Teil A')],
        document_nr='26F200'))

    assert tmp_db.get_booking_by_id(bk)[16] == '26F200'
    html = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    assert '26F200<' in html or "26F200'" in html


def test_bank_changes_do_not_leak_into_rows(tmp_db):
    """Keine Vererbung: Text und Beleg-Nr. der Bankbewegung ändern lässt die
    Teilbuchungen unangetastet."""
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    bk = _bank_booking(tmp_db, acct)
    handle_add_transaction(tmp_db, _post(bk, acct, [
        ('', '-238.00', coa_a, '', '', 'BELEG_A', 'Eigener Zeilentext'),
    ]))
    child_id = list(_children(tmp_db, bk))[0]

    handle_add_transaction(tmp_db, _post(
        bk, acct, [(child_id, '-238.00', coa_a, '', '', 'BELEG_A', 'Eigener Zeilentext')],
        text='Neuer Bank-Verwendungszweck', document_nr='BANK-NEU'))

    c = tmp_db.get_booking_by_id(child_id)
    assert c[16] == 'BELEG_A'
    assert c[15] == 'Eigener Zeilentext'


def test_doppik_mirror_survives_editor_save(tmp_db):
    """Reine Doppik-Spiegel stehen nicht im Editor und werden beim Speichern
    weder verändert noch gelöscht."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    bk = _bank_booking(tmp_db, acct)
    mirror = tmp_db.insert_booking('2026-03-01', -238.00, coa_id=bank_coa,
                                   counter_coa_id=bank_coa, booking_type='entry',
                                   parent_booking_id=bk)

    handle_add_transaction(tmp_db, _post(bk, acct, [
        ('', '-238.00', coa_a, '', '', 'A', 'Teil A'),
    ]))

    kids = _children(tmp_db, bk)
    assert mirror in kids
    assert kids[mirror][1] == bank_coa and kids[mirror][2] == bank_coa


def test_income_orientation_preserved_in_editor(tmp_db):
    """Liquide-zuerst gebuchte Einnahme: Editor zeigt das Erlöskonto und das
    Speichern dreht die Buchungsrichtung nicht um."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa_erloes = tmp_db.get_coa_id_by_account_number(4400)
    coa_neu = tmp_db.get_coa_id_by_account_number(4185)
    bk = _bank_booking(tmp_db, acct, amount=1190.00)
    child = tmp_db.insert_booking('2026-03-01', 1190.00, coa_id=bank_coa,
                                  counter_coa_id=coa_erloes, booking_type='entry',
                                  parent_booking_id=bk)

    html = PageTransactions(tmp_db, edit_transaction_id=bk)
    assert f'value="{coa_erloes}" selected' in html   # Zweckkonto, nicht Bank

    handle_add_transaction(tmp_db, _post(
        bk, acct, [(child, '1190.00', coa_neu, '', '', '', '')],
        amount='1190.00'))

    c = tmp_db.get_booking_by_id(child)
    assert c[8] == bank_coa        # liquide Seite bleibt vorn
    assert c[9] == coa_neu         # Zweckkonto auf der Gegenseite


def test_adopt_existing_booking_by_id(tmp_db):
    """Sammelüberweisung: bestehende Buchungen mit EIGENEN Belegnummern
    werden über ihre ID zugeordnet – ohne gemeinsame Beleg-Klammer."""
    acct = _bank_account(tmp_db)
    bank_coa = tmp_db.get_coa_id_by_account_number(1800)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = _bank_booking(tmp_db, acct, amount=-300.00)
    # zwei eigenständig erfasste Rechnungen, eigenes Datum und eigener Empfänger
    r1 = tmp_db.insert_booking('2026-02-20', -100.00, coa_id=coa,
                               counter_coa_id=bank_coa, document_number='RG-1',
                               recipient_client='Lieferant A',
                               text='Rechnung 1', booking_type='entry')
    r2 = tmp_db.insert_booking('2026-02-25', -200.00, coa_id=coa,
                               counter_coa_id=bank_coa, document_number='RG-2',
                               recipient_client='Lieferant B',
                               text='Rechnung 2', booking_type='entry')

    # beide erscheinen als Kandidaten in der Maske
    html = PageTransactions(tmp_db, edit_transaction_id=bk)
    assert 'vorhandene Buchung zuordnen' in html
    assert f'value="{r1}"' in html and f'value="{r2}"' in html

    handle_add_transaction(tmp_db, _post(bk, acct, [
        (r1, '-100.00', coa, '', '', 'RG-1', 'Rechnung 1'),
        (r2, '-200.00', coa, '', '', 'RG-2', 'Rechnung 2'),
    ], amount='-300.00'))

    kids = _children(tmp_db, bk)
    assert set(kids) == {r1, r2}                    # per ID zugeordnet
    b1 = tmp_db.get_booking_by_id(r1)
    assert b1[1] == '2026-02-20'                    # eigenes Datum bleibt
    assert b1[6] == 'Lieferant A'                   # eigener Empfänger bleibt
    assert b1[16] == 'RG-1'                         # eigene Beleg-Nr. bleibt


def test_blank_fields_keep_rows_aligned(tmp_db):
    """Leere Felder dürfen die Zeilen nicht verschieben.

    Zeile 1 ohne Steuer, Zeile 2 mit: fiele das leere Feld beim Parsen weg,
    landete der Steuerbetrag der zweiten Zeile auf der ersten – ein stiller
    Fehler in der Buchhaltung. Der Test geht denselben Weg wie der Server
    (urlencode → parse_qs).
    """
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    coa_b = tmp_db.get_coa_id_by_account_number(6300)
    bk = _bank_booking(tmp_db, acct)

    post = _post(bk, acct, [
        ('', '-200.00', coa_a, '', '', '', 'ohne Steuer'),
        ('', '-38.00', coa_b, 19, '-6.07', 'B', 'mit Steuer'),
    ])
    handle_add_transaction(tmp_db, _through_server_parser(post))

    kids = {c[1]: c for c in tmp_db.get_child_bookings_for_bank(bk)}
    assert kids[coa_a][5] is None and kids[coa_a][6] is None   # Satz/Betrag leer
    assert kids[coa_b][5] == 0.19
    assert kids[coa_b][6] == Decimal('-6.0700')
    assert kids[coa_b][7] == 'B' and kids[coa_a][7] is None    # Beleg-Nr.


def test_adopt_candidates_exclude_linked_bookings(tmp_db):
    """Bereits zugeordnete Buchungen tauchen nicht als Kandidat auf."""
    acct = _bank_account(tmp_db)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = _bank_booking(tmp_db, acct)
    child = tmp_db.insert_booking('2026-03-01', -238.00, coa_id=coa,
                                  booking_type='entry', parent_booking_id=bk)
    free = tmp_db.insert_booking('2026-03-02', -50.00, coa_id=coa,
                                 booking_type='entry')

    ids = {c[0] for c in tmp_db.get_unlinked_entry_bookings()}
    assert free in ids and child not in ids


def test_migration_releases_booking_groups(tmp_db, tmp_path):
    """Migration v5: Altbestand wird aus den Gruppen gelöst, die Buchungen
    bleiben unangetastet – und die Datenbank bleibt beschreibbar.

    Die Gruppen-Tabelle wird bewusst nur geleert, nicht gelöscht: In alten
    Datenbanken zeigt der Fremdschlüssel der Bookings-Tabelle weiterhin auf
    sie, und SQLite lehnt sonst jedes INSERT mit "no such table" ab.
    """
    import sqlite3
    from db import Database

    db_file = str(tmp_path / 'legacy.db')
    con = sqlite3.connect(db_file)
    con.executescript('''
        CREATE TABLE Invoices (ID INTEGER PRIMARY KEY);
        CREATE TABLE BookingGroups (ID INTEGER PRIMARY KEY AUTOINCREMENT,
            Description TEXT, CreatedDate DATE, TotalAmount INTEGER);
        CREATE TABLE Bookings (ID INTEGER PRIMARY KEY AUTOINCREMENT,
            DateBooking DATE, DateTax DATE, BookingGroup_ID INTEGER,
            Account_ID INTEGER, ForeignBankAccount TEXT, RecipientClient TEXT,
            Contact_ID INTEGER, COA_ID INTEGER, CounterCOA_ID INTEGER,
            Category_ID INTEGER, Amount INTEGER, Currency TEXT, TaxRate REAL,
            TaxAmount INTEGER, Text TEXT, DocumentNumber TEXT,
            BookingType TEXT, ParentBooking_ID INTEGER, Status TEXT,
            AutoMirror INTEGER DEFAULT 0,
            FOREIGN KEY (BookingGroup_ID) REFERENCES BookingGroups(ID));
        INSERT INTO BookingGroups (Description) VALUES ('Alt-Split');
        INSERT INTO Bookings (DateBooking, BookingGroup_ID, Amount, Text,
                              DocumentNumber, BookingType)
            VALUES ('2025-01-05', 1, 1000000, 'Teil A', 'ALT-1', 'entry');
        INSERT INTO Bookings (DateBooking, BookingGroup_ID, Amount, Text,
                              DocumentNumber, BookingType)
            VALUES ('2025-01-05', 1, 500000, 'Teil B', 'ALT-1', 'entry');
        PRAGMA user_version = 4;
    ''')
    con.commit()
    con.close()

    db = Database(db_name=db_file)     # löst die Migration aus

    con = sqlite3.connect(db_file)
    assert con.execute('PRAGMA user_version').fetchone()[0] == 5
    assert con.execute('SELECT COUNT(*) FROM BookingGroups').fetchone()[0] == 0
    rows = con.execute('SELECT Text, DocumentNumber, BookingGroup_ID, Amount'
                       ' FROM Bookings ORDER BY ID').fetchall()
    con.close()
    assert [r[0] for r in rows] == ['Teil A', 'Teil B']   # Buchungen erhalten
    assert all(r[1] == 'ALT-1' for r in rows)             # Beleg-Klammer bleibt
    assert all(r[2] is None for r in rows)                # Gruppe gelöst
    assert [r[3] for r in rows] == [1000000, 500000]      # Beträge unverändert

    # Entscheidend: Die migrierte Datenbank muss weiter beschreibbar sein
    new_id = db.insert_booking('2026-02-02', -12.34, text='Nach Migration',
                               booking_type='entry')
    assert db.get_booking_by_id(new_id)[15] == 'Nach Migration'


def test_editor_rendered_only_for_bank_bookings(tmp_db):
    """Buchungen ohne Bankbezug behalten die klassischen Kontierungsfelder."""
    acct = _bank_account(tmp_db)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = _bank_booking(tmp_db, acct)
    entry = tmp_db.insert_booking('2026-03-01', -50.00, coa_id=coa,
                                  booking_type='entry')

    bank_html = PageTransactions(tmp_db, edit_transaction_id=bk)
    assert 'Buchungssätze zu dieser Bankbewegung' in bank_html
    assert 'name="split_amount"' in bank_html
    assert 'name="tax_rate"' not in bank_html           # Steuer je Teilbuchung
    assert 'name="document_nr"' in bank_html            # Beleg-Nr. bleibt oben
    assert 'name="booking_group_id"' not in bank_html   # Gruppen abgelöst

    entry_html = PageTransactions(tmp_db, edit_transaction_id=entry)
    assert 'Buchungssätze zu dieser Bankbewegung' not in entry_html
    assert 'name="coa_id"' in entry_html
    assert 'name="booking_group_id"' not in entry_html
