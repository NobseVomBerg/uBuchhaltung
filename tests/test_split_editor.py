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

from server.handlers import handle_add_transaction
from server.pages_transactions import PageTransactions


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
    als offener Rest ausgewiesen; nach Ergänzen erscheint der Haken."""
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
    assert 'Rest -38.00' in html
    assert 'Bank + Buchungssätze vollständig' not in html

    first_id = list(_children(tmp_db, bk))[0]
    handle_add_transaction(tmp_db, _post(bk, acct, [
        (first_id, '-200.00', coa_a, '', '', 'A', 'Teil A'),
        ('', '-38.00', coa_b, '', '', 'B', 'Teil B'),
    ]))
    html = PageTransactions(tmp_db, date_from='2026-01-01', date_to='2026-12-31')
    assert 'Bank + Buchungssätze vollständig' in html
    assert 'Rest -38.00' not in html


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
    assert 'name="booking_group_id"' not in bank_html   # Gruppen-Dropdown weg
    assert 'name="tax_rate"' not in bank_html           # Steuer je Teilbuchung
    assert 'name="document_nr"' in bank_html            # Beleg-Nr. bleibt oben

    entry_html = PageTransactions(tmp_db, edit_transaction_id=entry)
    assert 'Buchungssätze zu dieser Bankbewegung' not in entry_html
    assert 'name="coa_id"' in entry_html
    assert 'name="booking_group_id"' in entry_html
