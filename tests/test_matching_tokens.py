# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Auto-Abgleich: Unterscheidung gleichbetragiger Bewegungen über Kennungen.

Zwei Lastschriften desselben Händlers am selben Tag über denselben Betrag
lassen sich weder über Datum noch Betrag noch Empfänger auseinanderhalten.
Was bleibt, ist die Transaktionskennung im Verwendungszweck – und die ist bei
manchen Zahlungsdienstleistern **alphanumerisch**, nicht rein numerisch.

Beträge und Kennungen sind erfunden.
"""


def _bank(db, account_id, datum, betrag, text):
    return db.insert_booking(date_booking=datum, amount=betrag,
                             account_id=account_id, recipient_client='Haendler',
                             text=text, booking_type='bank')


def _entry(db, datum, betrag, text, coa, counter):
    return db.insert_booking(date_booking=datum, amount=betrag, coa_id=coa,
                             counter_coa_id=counter, text=text,
                             booking_type='entry')


def _coa(db, nummer):
    conn = db._get_connection()
    row = conn.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?',
                       (nummer,)).fetchone()
    conn.close()
    return row[0]


def _kinder(db, bank_id):
    conn = db._get_connection()
    rows = [r[0] for r in conn.execute(
        'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (bank_id,))]
    conn.close()
    return rows


def test_alphanumerische_kennung_trennt_gleichbetragige_lastschriften(
        db_with_coa):
    """Der numerische Teil ist bei beiden gleich (dieselbe Bestellnummer);
    nur die alphanumerische Transaktionskennung unterscheidet sie."""
    konto = next(a[0] for a in db_with_coa.fetch_accounts()
                 if a[1] == 'Testkonto 2')                    # SKR 1810
    privat, bank = _coa(db_with_coa, 2100), _coa(db_with_coa, 1810)

    bank_a = _bank(db_with_coa, konto, '2024-09-25', -106.38,
                   '306-0632285-5909113 Haendler 2BSTZZGVGU16KNKI')
    bank_b = _bank(db_with_coa, konto, '2024-09-25', -106.38,
                   '306-0632285-5909113 Haendler 4O6SB5GEIDBA9Y74')
    entry_a = _entry(db_with_coa, '2024-09-25', -106.38,
                     '306-0632285-5909113 Haendler 2BSTZZGVGU16KNKI EREF: '
                     '2BSTZZGVGU16KNKI', privat, bank)
    entry_b = _entry(db_with_coa, '2024-09-25', -106.38,
                     '306-0632285-5909113 Haendler 4O6SB5GEIDBA9Y74 EREF: '
                     '4O6SB5GEIDBA9Y74', privat, bank)

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, bank_a) == [entry_a]
    assert _kinder(db_with_coa, bank_b) == [entry_b]


def test_reine_ziffernkennung_greift_weiterhin(db_with_coa):
    """Die bisherige Erkennung darf nicht verloren gehen – auch nicht, wenn
    die Ziffernfolge im Buchungssatz an einem Wort klebt."""
    konto = next(a[0] for a in db_with_coa.fetch_accounts()
                 if a[1] == 'Testkonto 2')
    aufwand, bank = _coa(db_with_coa, 6815), _coa(db_with_coa, 1810)

    bank_a = _bank(db_with_coa, konto, '2024-05-02', -49.90, 'Beleg 1040749116593')
    bank_b = _bank(db_with_coa, konto, '2024-05-02', -49.90, 'Beleg 2251887730044')
    entry_a = _entry(db_with_coa, '2024-05-02', -49.90,
                     'Rechnung1040749116593 Buerobedarf', aufwand, bank)
    entry_b = _entry(db_with_coa, '2024-05-02', -49.90,
                     'Rechnung2251887730044 Buerobedarf', aufwand, bank)

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, bank_a) == [entry_a]
    assert _kinder(db_with_coa, bank_b) == [entry_b]


def test_ein_wort_ohne_ziffer_sticht_die_echte_kennung_nicht(db_with_coa):
    """Nur Folgen **mit Ziffer** gelten als Kennung.

    Sonst zählte das gemeinsame Wort „Buerobedarfshop“ genauso viel wie die
    Transaktionsnummer – beide Kandidaten stünden gleichauf, und die Zuordnung
    fiele auf die Textähnlichkeit zurück, die hier den falschen wählt.
    """
    konto = next(a[0] for a in db_with_coa.fetch_accounts()
                 if a[1] == 'Testkonto 2')
    aufwand, bank = _coa(db_with_coa, 6815), _coa(db_with_coa, 1810)

    bank_id = _bank(db_with_coa, konto, '2024-06-03', -20.00,
                    'Buerobedarfshop 2251887730044')
    _entry(db_with_coa, '2024-06-03', -20.00,
           'Buerobedarfshop Sammelbestellung', aufwand, bank)   # nur das Wort
    richtig = _entry(db_with_coa, '2024-06-03', -20.00,
                     'Beleg 2251887730044', aufwand, bank)      # die Kennung

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, bank_id) == [richtig]
