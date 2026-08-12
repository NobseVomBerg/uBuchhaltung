# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Auto-Abgleich: eine Bankbewegung, mehrere Quellgruppen.

WISO zerlegt manche Belege in mehrere Vorgänge mit je eigener ACCOUNTINGID:
ein Stromabschlag in betrieblichen Anteil und Privatanteil, eine
Kartenabrechnung in ihre Einzelposten, eine zu knappe Überweisung in
Teilzahlungen auf zwei Rechnungen. Keine Gruppe trifft dann den Bankbetrag –
ihre Summe aber genau.

Beträge und Namen sind erfunden.
"""


def _coa(db, nummer):
    conn = db._get_connection()
    row = conn.execute('SELECT ID FROM ChartOfAccounts WHERE AccountNumber = ?',
                       (nummer,)).fetchone()
    conn.close()
    return row[0]


def _konto(db, name='Testkonto 2'):
    return next(a[0] for a in db.fetch_accounts() if a[1] == name)


def _bank(db, konto, datum, betrag, empfaenger, text=''):
    return db.insert_booking(date_booking=datum, amount=betrag,
                             account_id=konto, recipient_client=empfaenger,
                             text=text, booking_type='bank')


def _gruppe(db, gruppe, datum, zeilen):
    """Buchungssätze einer Quellgruppe anlegen; zeilen = (coa, gegen, betrag, text)."""
    ids = []
    for coa, gegen, betrag, text in zeilen:
        ids.append(db.insert_booking(date_booking=datum, amount=betrag,
                                     coa_id=coa, counter_coa_id=gegen,
                                     text=text, booking_type='entry'))
    conn = db._get_connection()
    conn.executemany('UPDATE Bookings SET SourceGroup = ? WHERE ID = ?',
                     [(gruppe, i) for i in ids])
    conn.commit()
    conn.close()
    return ids


def _kinder(db, bank_id):
    conn = db._get_connection()
    rows = sorted(r[0] for r in conn.execute(
        'SELECT ID FROM Bookings WHERE ParentBooking_ID = ?', (bank_id,)))
    conn.close()
    return rows


def test_betrieblicher_anteil_und_privatanteil_zusammen(db_with_coa):
    """Stromabschlag: WISO bucht Betriebsanteil und Privatanteil getrennt."""
    bank_coa = _coa(db_with_coa, 1810)
    strom, privat = _coa(db_with_coa, 6325), _coa(db_with_coa, 2100)

    bank_id = _bank(db_with_coa, _konto(db_with_coa), '2022-02-01', -131.00,
                    'Energieversorger AG', 'Abschlag Strom Januar')
    a = _gruppe(db_with_coa, '27095', '2022-02-01',
                [(strom, bank_coa, -98.25, 'Abschlag Strom Januar')])
    b = _gruppe(db_with_coa, '27096', '2022-02-01',
                [(privat, bank_coa, -32.75, 'Abschlag Strom Januar')])

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, bank_id) == sorted(a + b)


def test_teilzahlung_auf_zwei_rechnungen(db_with_coa):
    """Zu knapp überwiesen: der Betrag verteilt sich auf zwei Rechnungen."""
    bank_coa, debitor = _coa(db_with_coa, 1810), _coa(db_with_coa, 10000)
    offen, erloes = _coa(db_with_coa, 4405), _coa(db_with_coa, 4400)

    bank_id = _bank(db_with_coa, _konto(db_with_coa), '2022-05-20', 27.20,
                    'Dauerkunde', 'Ohne Rechnungsnummer')
    a = _gruppe(db_with_coa, '22159', '2022-05-20',
                [(bank_coa, debitor, 4.10, 'Zahlung zu Re. 2022003'),
                 (offen, erloes, 4.10, 'Umb. zu Re. 2022003')])
    b = _gruppe(db_with_coa, '22160', '2022-05-20',
                [(bank_coa, debitor, 23.10, 'Zahlung zu Re. 2022005'),
                 (offen, erloes, 23.10, 'Umb. zu Re. 2022005')])

    db_with_coa.link_bank_to_entries()

    # Die Umbuchungen sind nicht bankwirksam, gehören aber zum Vorgang.
    assert _kinder(db_with_coa, bank_id) == sorted(a + b)


def test_zwei_bankbewegungen_am_selben_tag_werden_getrennt(db_with_coa):
    """Der Empfänger im Buchungstext entscheidet, welche Gruppe wohin gehört."""
    bank_coa = _coa(db_with_coa, 1810)
    strom, privat = _coa(db_with_coa, 6325), _coa(db_with_coa, 2100)
    bedarf = _coa(db_with_coa, 6850)

    strom_bank = _bank(db_with_coa, _konto(db_with_coa), '2022-05-02', -131.00,
                       'Energieversorger AG', 'Abschlag Strom April')
    karte_bank = _bank(db_with_coa, _konto(db_with_coa), '2022-05-02', -408.11,
                       'Kartenabrechner AG', 'Kartenabrechnung April')
    s1 = _gruppe(db_with_coa, '27144', '2022-05-02',
                 [(strom, bank_coa, -98.25, 'Abschlag Strom April')])
    s2 = _gruppe(db_with_coa, '27145', '2022-05-02',
                 [(privat, bank_coa, -32.75, 'Abschlag Strom April')])
    k1 = _gruppe(db_with_coa, '27169', '2022-05-02',
                 [(bedarf, bank_coa, -200.00, 'Einkauf Kartenabrechner AG')])
    k2 = _gruppe(db_with_coa, '27170', '2022-05-02',
                 [(bedarf, bank_coa, -208.11, 'Tanken Kartenabrechner AG')])

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, strom_bank) == sorted(s1 + s2)
    assert _kinder(db_with_coa, karte_bank) == sorted(k1 + k2)


def test_nachlauf_loest_die_reihenfolge_auf(db_with_coa):
    """Beide Bewegungen sehen zuerst alle Gruppen des Tages.

    „Beta“ steht in keinem Buchungstext, ist also allein nicht auflösbar.
    Sobald „Alpha“ über seinen Empfänger verknüpft ist, bleibt für „Beta“
    genau der Rest übrig – dafür gibt es den Nachlauf. Ohne ihn bliebe die
    Bewegung offen, weil sie vor Alpha geprüft wird.
    """
    bank_coa = _coa(db_with_coa, 1810)
    bedarf, privat = _coa(db_with_coa, 6850), _coa(db_with_coa, 2100)

    alpha = _bank(db_with_coa, _konto(db_with_coa), '2022-06-15', -30.00,
                  'Alpha', 'ohne Kennung')
    beta = _bank(db_with_coa, _konto(db_with_coa), '2022-06-15', -70.00,
                 'Beta', 'ohne Kennung')
    a1 = _gruppe(db_with_coa, '30001', '2022-06-15',
                 [(bedarf, bank_coa, -20.00, 'Einkauf bei Alpha')])
    a2 = _gruppe(db_with_coa, '30002', '2022-06-15',
                 [(privat, bank_coa, -10.00, 'Privatanteil Alpha')])
    b1 = _gruppe(db_with_coa, '30003', '2022-06-15',
                 [(bedarf, bank_coa, -45.00, 'Posten drei')])
    b2 = _gruppe(db_with_coa, '30004', '2022-06-15',
                 [(privat, bank_coa, -25.00, 'Posten vier')])

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, alpha) == sorted(a1 + a2)
    assert _kinder(db_with_coa, beta) == sorted(b1 + b2)


def test_ohne_passende_summe_wird_nichts_verknuepft(db_with_coa):
    """Geht die Summe nicht auf, bleibt die Bewegung offen statt geraten."""
    bank_coa = _coa(db_with_coa, 1810)
    bedarf, privat = _coa(db_with_coa, 6850), _coa(db_with_coa, 2100)

    bank_id = _bank(db_with_coa, _konto(db_with_coa), '2022-08-09', -500.00,
                    'Unbekannt', 'ohne Kennung')
    _gruppe(db_with_coa, '31001', '2022-08-09',
            [(bedarf, bank_coa, -120.00, 'Posten eins')])
    _gruppe(db_with_coa, '31002', '2022-08-09',
            [(privat, bank_coa, -35.00, 'Posten zwei')])

    db_with_coa.link_bank_to_entries()

    assert _kinder(db_with_coa, bank_id) == []
