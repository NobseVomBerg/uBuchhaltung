# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Bank-Buchung bearbeiten: der Buchungssatz (entry-Kind) zieht mit.

Regression: Beim ERSTEN Speichern mit SKR-Konto entstand das entry-Kind,
bei jedem WEITEREN Speichern wurde nur die Bank-Zeile aktualisiert. Da die
Übersicht (fetch_bookings_grouped) SKR-Konto und Steuersatz aus dem Kind
liest – und die EÜR ausschließlich mit den Kindern rechnet – kamen
Änderungen dort nie an.

Mitgezogen wird ausschließlich der von uBuchhaltung selbst angelegte
Spiegel (Bookings.AutoMirror=1). Eigenständig erfasste Buchungen, die nur
über ParentBooking_ID mit der Bankbewegung verknüpft sind (WISO-Import,
Handerfassung, Debitoren-Zahlungen), folgen einer eigenen Kontierungslogik
und bleiben unangetastet – ihre Herkunft ließe sich aus den Werten allein
nicht rekonstruieren.
"""
from decimal import Decimal

import pytest

from server.handlers import handle_add_transaction


def _bank_account(db, name='Sync-Bank', skr=1800):
    db.insert_account(name, 'Owner', 'DE00SYNC', 'BIC', 'Bank',
                      is_cash=0, skr_account=skr)
    return [a for a in db.fetch_accounts() if a[1] == name][0][0]


def _post(booking_id, account_id, coa_id, **kw):
    data = {'transaction_id': [str(booking_id)], 'date': ['2026-03-01'],
            'amount': ['-119.00'], 'account': [str(account_id)],
            'coa_id': [str(coa_id) if coa_id else ''], 'currency': ['EUR']}
    data.update({k: [str(v)] for k, v in kw.items()})
    return data


def _bank_row(db, booking_id):
    """Zeile der Übersicht zu dieser Bank-Buchung (merged view)."""
    rows = db.fetch_bookings_grouped('2026-01-01', '2026-12-31')
    return next(r for r in rows
                if r['type'] == 'bank' and r['booking'][0] == booking_id)


# ── Der gemeldete Ablauf ─────────────────────────────────────────────────────

def test_second_edit_reaches_overview(tmp_db):
    acct = _bank_account(tmp_db)
    coa_6815 = tmp_db.get_coa_id_by_account_number(6815)
    coa_6850 = tmp_db.get_coa_id_by_account_number(6850)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               recipient_client='Lieferant', text='Kauf',
                               booking_type='bank')

    # Schritt 2: SKR 6815 setzen → Buchungssatz entsteht
    handle_add_transaction(tmp_db, _post(bk, acct, coa_6815))
    assert len(tmp_db.get_child_bookings_for_bank(bk)) == 1
    assert _bank_row(tmp_db, bk)['entry_coa_id'] == coa_6815

    # Schritt 3: SKR 6850 + 19 % — früher blieb das Kind auf 6815/ohne Steuer
    handle_add_transaction(tmp_db, _post(bk, acct, coa_6850, tax_rate=19,
                                         tax_amount=19.00))

    # Schritt 4: Übersicht zeigt die Änderung
    row = _bank_row(tmp_db, bk)
    assert row['entry_coa_id'] == coa_6850
    assert row['entry_tax_rate'] == 0.19
    assert len(tmp_db.get_child_bookings_for_bank(bk)) == 1
    assert tmp_db.get_booking_by_id(bk)[8] == coa_6850


def test_euer_uses_updated_child(tmp_db):
    """EÜR folgt der Korrektur: Betrag landet auf dem neuen Konto."""
    acct = _bank_account(tmp_db)
    coa_6815 = tmp_db.get_coa_id_by_account_number(6815)
    coa_6850 = tmp_db.get_coa_id_by_account_number(6850)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               booking_type='bank')
    handle_add_transaction(tmp_db, _post(bk, acct, coa_6815))
    handle_add_transaction(tmp_db, _post(bk, acct, coa_6850))

    euer = {nr: total for nr, _, total in
            tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert 6815 not in euer
    assert euer.get(6850) == -119.0


def test_counter_coa_follows_account_change(tmp_db):
    """Kontowechsel setzt das Gegenkonto des Buchungssatzes neu."""
    acct1 = _bank_account(tmp_db, 'Bank-1', skr=1800)
    acct2 = _bank_account(tmp_db, 'Bank-2', skr=1810)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct1,
                               booking_type='bank')
    handle_add_transaction(tmp_db, _post(bk, acct1, coa))
    child_id = tmp_db.get_child_bookings_for_bank(bk)[0][0]
    assert tmp_db.get_booking_by_id(child_id)[9] == \
        tmp_db.get_coa_id_by_account_number(1800)

    handle_add_transaction(tmp_db, _post(bk, acct2, coa))
    assert tmp_db.get_booking_by_id(child_id)[9] == \
        tmp_db.get_coa_id_by_account_number(1810)


# ── Schutz eigenständig erfasster / anders orientierter Buchungssätze ────────

def test_income_orientation_is_preserved(tmp_db):
    """Einnahme-Buchungssatz (COA = Bank, Gegenkonto = Erlöse) darf nicht zum
    Doppik-Spiegel werden – sonst verschwinden Erlöse aus der EÜR."""
    acct = _bank_account(tmp_db)
    coa_bank = tmp_db.get_coa_id_by_account_number(1800)
    coa_erloes = tmp_db.get_coa_id_by_account_number(4400)
    bk = tmp_db.insert_booking('2026-03-01', 1190.00, account_id=acct,
                               booking_type='bank')
    child = tmp_db.insert_booking('2026-03-01', 1190.00, coa_id=coa_bank,
                                  counter_coa_id=coa_erloes,
                                  booking_type='entry', parent_booking_id=bk)  # nicht auto-erzeugt

    # Maske liefert bei dieser Orientierung das Erlöskonto zurück
    handle_add_transaction(tmp_db, _post(bk, acct, coa_erloes, amount='1190.00'))

    c = tmp_db.get_booking_by_id(child)
    assert c[8] == coa_bank and c[9] == coa_erloes    # Orientierung erhalten
    euer = {nr: total for nr, _, total in
            tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert euer.get(4400) == 1190.0                   # Erlöse bleiben in der EÜR
    assert len(tmp_db.get_child_bookings_for_bank(bk)) == 1


def test_repeated_saves_do_not_multiply_children(tmp_db):
    """Wiederholtes Speichern legt kein weiteres Kind an (auch nicht bei
    Doppik-Spiegel-Kindern, die aus der gefilterten Sicht 'fehlen')."""
    acct = _bank_account(tmp_db)
    coa_bank = tmp_db.get_coa_id_by_account_number(1800)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               booking_type='bank')
    tmp_db.insert_booking('2026-03-01', -119.00, coa_id=coa_bank,
                          counter_coa_id=coa_bank,   # reiner Doppik-Spiegel
                          booking_type='entry', parent_booking_id=bk)

    for _ in range(4):
        handle_add_transaction(tmp_db, _post(bk, acct, coa))
    assert len(tmp_db.get_child_bookings_for_bank(bk)) == 1


def test_linked_booking_stays_untouched(tmp_db):
    """Eine eigenständig erfasste, nur verknüpfte Buchung wird nie angefasst –
    auch dann nicht, wenn Betrag, Datum und Kontierung exakt zur Bank-Zeile
    passen (genau das erzeugt das Auto-Linking über Datum + Betrag)."""
    acct = _bank_account(tmp_db)
    coa_bank = tmp_db.get_coa_id_by_account_number(1800)
    coa_alt = tmp_db.get_coa_id_by_account_number(6815)
    coa_neu = tmp_db.get_coa_id_by_account_number(6850)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               coa_id=coa_alt,   # Bank-Zeile bereits kontiert
                               recipient_client='ROH-BANKTEXT AG',
                               text='SEPA-Lastschrift Rohdaten',
                               booking_type='bank')
    child = tmp_db.insert_booking(
        '2026-03-01', -119.00, date_tax='2026-02-25', coa_id=coa_alt,
        counter_coa_id=coa_bank, tax_rate=0.19, tax_amount=-19.00,
        recipient_client='Sauber erfasster Lieferant',
        text='Rechnung 4711 Büromaterial', document_number='R-4711',
        booking_type='entry', parent_booking_id=bk)  # nicht auto-erzeugt

    # zweimal speichern – früher schlug der Sync ab dem zweiten Mal zu
    for _ in range(2):
        handle_add_transaction(tmp_db, _post(bk, acct, coa_neu, tax_rate=7,
                                             tax_amount=-7.79,
                                             document_nr='RG-999',
                                             recipient='ROH-BANKTEXT AG',
                                             text='SEPA-Lastschrift Rohdaten'))

    c = tmp_db.get_booking_by_id(child)
    assert c[8] == coa_alt                    # Kontierung unberührt
    assert c[2] == '2026-02-25'               # Steuerdatum unberührt
    assert c[6] == 'Sauber erfasster Lieferant'
    assert c[13] == 0.19                      # Steuersatz unberührt
    assert c[14] == Decimal('-19.0000')       # Steuerbetrag unberührt
    assert c[15] == 'Rechnung 4711 Büromaterial'
    assert c[16] == 'R-4711'                  # Beleg-Nr. unberührt
    assert c[11] == Decimal('-119.0000')


def test_transfer_without_liquid_side_untouched(tmp_db):
    """Umbuchung ohne liquide Seite (4405 → 4400) behält ihr Gegenkonto."""
    acct = _bank_account(tmp_db)
    coa_4405 = tmp_db.get_coa_id_by_account_number(4405)
    coa_4400 = tmp_db.get_coa_id_by_account_number(4400)
    bk = tmp_db.insert_booking('2026-03-01', 1190.00, account_id=acct,
                               booking_type='bank')
    child = tmp_db.insert_booking('2026-03-01', 1190.00, coa_id=coa_4405,
                                  counter_coa_id=coa_4400,
                                  booking_type='entry', parent_booking_id=bk)  # nicht auto-erzeugt

    handle_add_transaction(tmp_db, _post(bk, acct, coa_4405, amount='1190.00'))

    c = tmp_db.get_booking_by_id(child)
    assert (c[8], c[9]) == (coa_4405, coa_4400)
    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert euer.get(4400) == 1190.0


def test_debtor_payment_untouched(tmp_db):
    """Zahlung auf Debitor (COA=Bank, Gegenkonto=Debitoren) bleibt erhalten –
    sonst findet der Auto-Abgleich den Zahlungssatz nie wieder."""
    acct = _bank_account(tmp_db)
    coa_bank = tmp_db.get_coa_id_by_account_number(1800)
    coa_deb = tmp_db.get_coa_id_by_account_number(10000)
    coa_4400 = tmp_db.get_coa_id_by_account_number(4400)
    bk = tmp_db.insert_booking('2026-03-01', 1190.00, account_id=acct,
                               coa_id=coa_4400, booking_type='bank')
    child = tmp_db.insert_booking('2026-03-01', 1190.00, coa_id=coa_bank,
                                  counter_coa_id=coa_deb,
                                  booking_type='entry', parent_booking_id=bk)  # nicht auto-erzeugt

    handle_add_transaction(tmp_db, _post(bk, acct, coa_4400, amount='1190.00'))

    c = tmp_db.get_booking_by_id(child)
    assert (c[8], c[9]) == (coa_bank, coa_deb)


def test_bank_account_as_purpose_creates_no_mirror(tmp_db):
    """Wählt der Nutzer das Bankkonto selbst als SKR-Konto, darf daraus kein
    Doppik-Spiegel werden (die Buchung verschwände aus der EÜR)."""
    acct = _bank_account(tmp_db)
    coa_bank = tmp_db.get_coa_id_by_account_number(1800)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               booking_type='bank')
    handle_add_transaction(tmp_db, _post(bk, acct, coa))
    child_id = tmp_db.get_child_bookings_for_bank(bk)[0][0]

    handle_add_transaction(tmp_db, _post(bk, acct, coa_bank))

    c = tmp_db.get_booking_by_id(child_id)
    assert (c[8], c[9]) != (coa_bank, coa_bank)
    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert euer.get(6815) == -119.0


def test_amount_and_tax_stay_coherent(tmp_db):
    """Betrag und Steuerbetrag wandern gemeinsam – sonst rechnet die EÜR
    (Netto = Betrag − Steuer) mit einem erfundenen Wert."""
    acct = _bank_account(tmp_db)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-03-01', -238.00, account_id=acct,
                               booking_type='bank')
    handle_add_transaction(tmp_db, _post(bk, acct, coa, amount='-238.00',
                                         tax_rate=19, tax_amount=-38.00))
    child_id = tmp_db.get_child_bookings_for_bank(bk)[0][0]

    # Betrag korrigieren (die Maske rechnet den Steuerbetrag neu)
    handle_add_transaction(tmp_db, _post(bk, acct, coa, amount='-500.00',
                                         tax_rate=19, tax_amount=-79.83))

    c = tmp_db.get_booking_by_id(child_id)
    assert c[11] == Decimal('-500.0000')
    assert c[14] == Decimal('-79.8300')
    euer = {nr: t for nr, _, t in tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert euer.get(6815) == pytest.approx(-420.17, abs=0.01)
    assert euer.get(1406) == pytest.approx(-79.83, abs=0.01)


def test_clearing_skr_leaves_child_untouched(tmp_db):
    """SKR-Konto leeren darf den Buchungssatz nicht entkontieren (der Betrag
    fiele sonst still aus der EÜR)."""
    acct = _bank_account(tmp_db)
    coa = tmp_db.get_coa_id_by_account_number(6815)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               booking_type='bank')
    handle_add_transaction(tmp_db, _post(bk, acct, coa))
    child_id = tmp_db.get_child_bookings_for_bank(bk)[0][0]

    handle_add_transaction(tmp_db, _post(bk, acct, None))

    assert tmp_db.get_booking_by_id(child_id)[8] == coa
    euer = {nr: total for nr, _, total in
            tmp_db.get_euer_data('2026-01-01', '2026-12-31')}
    assert euer.get(6815) == -119.0


def test_account_without_skr_keeps_counter_coa(tmp_db):
    """Bankkonto ohne SKR-Nummer: Das Kind ist kein erkennbarer Spiegel,
    sein Gegenkonto bleibt erhalten (sonst verlöre die EÜR die Richtung)."""
    db = tmp_db
    db.insert_account('Ohne-SKR', 'Owner', 'DE00NOSKR', 'BIC', 'Bank',
                      is_cash=0, skr_account=None)
    acct = [a for a in db.fetch_accounts() if a[1] == 'Ohne-SKR'][0][0]
    coa_bank = db.get_coa_id_by_account_number(1800)
    coa_alt = db.get_coa_id_by_account_number(6815)
    coa_neu = db.get_coa_id_by_account_number(6850)
    bk = db.insert_booking('2026-03-01', -119.00, account_id=acct,
                           booking_type='bank')
    child = db.insert_booking('2026-03-01', -119.00, coa_id=coa_alt,
                              counter_coa_id=coa_bank, booking_type='entry',
                              parent_booking_id=bk)

    handle_add_transaction(db, _post(bk, acct, coa_neu))

    c = db.get_booking_by_id(child)
    assert c[8] == coa_alt       # Kontierung unberührt
    assert c[9] == coa_bank      # Gegenkonto nicht auf NULL gesetzt


def test_split_children_are_left_alone(tmp_db):
    """Mehrere Buchungssatz-Kinder (Split): Aufteilung bleibt unangetastet."""
    acct = _bank_account(tmp_db)
    coa_a = tmp_db.get_coa_id_by_account_number(6815)
    coa_b = tmp_db.get_coa_id_by_account_number(6850)
    bk = tmp_db.insert_booking('2026-03-01', -119.00, account_id=acct,
                               booking_type='bank')
    c1 = tmp_db.insert_booking('2026-03-01', -100.00, coa_id=coa_a,
                               booking_type='entry', parent_booking_id=bk)
    c2 = tmp_db.insert_booking('2026-03-01', -19.00, coa_id=coa_b,
                               booking_type='entry', parent_booking_id=bk)

    handle_add_transaction(tmp_db, _post(bk, acct, coa_a))

    assert tmp_db.get_booking_by_id(c1)[8] == coa_a
    assert tmp_db.get_booking_by_id(c1)[11] == Decimal('-100.0000')
    assert tmp_db.get_booking_by_id(c2)[8] == coa_b
    assert tmp_db.get_booking_by_id(c2)[11] == Decimal('-19.0000')
    assert len(tmp_db.get_child_bookings_for_bank(bk)) == 2


# ── Bearbeiten-Maske ────────────────────────────────────────────────────────

def test_edit_form_shows_purpose_account_for_income(tmp_db):
    """Bei liquide-zuerst gebuchten Einnahmen zeigt die Maske das Erlöskonto,
    nicht das Bankkonto (sonst schriebe der Nutzer es blind zurück)."""
    from server.pages_transactions import PageTransactions
    acct = _bank_account(tmp_db)
    coa_bank = tmp_db.get_coa_id_by_account_number(1800)
    coa_erloes = tmp_db.get_coa_id_by_account_number(4400)
    bk = tmp_db.insert_booking('2026-03-01', 1190.00, account_id=acct,
                               booking_type='bank')
    tmp_db.insert_booking('2026-03-01', 1190.00, coa_id=coa_bank,
                          counter_coa_id=coa_erloes, booking_type='entry',
                          parent_booking_id=bk)

    html = PageTransactions(tmp_db, edit_transaction_id=bk)
    assert f'value="{coa_erloes}" selected' in html
    assert f'value="{coa_bank}" selected' not in html
