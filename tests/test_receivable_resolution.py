# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Eine Beleg-Nr. mit MEHREREN Forderungszeilen: Stufe 7 des Auto-Abgleichs und das Entfernen einer Zahlung.

Eine Rechnung bucht heute eine Forderungszeile („10000 an Wartekonto“); trägt eine Beleg-Nr. mehrere solche Zeilen (eine Rechnung mit
mehreren Steuersätzen bekommt später je Satz eine), darf keine Zeile für sich mit der Summe ALLER Zahlungen der Beleg-Nr. verglichen werden:
Die kleinere wäre nach einer Teilzahlung zu früh ausgeglichen (``Status = 'resolved'``). Maßgeblich ist die Summe ALLER eigenständigen
Forderungszeilen der Nummer (auch der schon aufgelösten) – ist sie gedeckt (``money.is_covered``), werden alle noch offenen Zeilen der Nummer
gemeinsam ``resolved``; ist sie nach dem Entfernen einer Zahlung nicht mehr gedeckt (``Database._after_payment_removed``), gehen alle aufgelösten
gemeinsam zurück auf offen. Bei genau einer Zeile ändert sich nichts (``tests/test_invoice_payment_assignment.py``,
``tests/test_single_rate_snapshots.py``).

Beträge, Namen und Nummern sind erfunden.
"""
import sqlite3

import pytest

from db.matching import FORM_TRANSFER

NUMBER = 'R-MIX'


# ── Hilfen ───────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_db):
    return tmp_db


@pytest.fixture
def acct(db):
    db.insert_account('Auflösungs-Bank', 'Ich', 'DE00AUFLOESUNG', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    return [a for a in db.fetch_accounts() if a[1] == 'Auflösungs-Bank'][0][0]


def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _raw(db, sql, *params):
    con = sqlite3.connect(db.db_name)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def _status(db, booking_id):
    return _raw(db, 'SELECT Status FROM Bookings WHERE ID = ?', booking_id)[0][0]


def _invoice(db, number, gross):
    """Eine versendete Rechnung über ``gross`` (Kopf mit 19 %) – ohne Forderungsbuchung: die Zeilen setzt der Test selbst."""
    net = round(gross / 1.19, 2)
    return db.insert_invoice({
        'invoice_number': number, 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH',
        'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': 0.19, 'sum_net': net, 'tax_amount': round(gross - net, 2),
        'sum_gross': gross, 'amount_due': gross, 'status': 'sent'})


def _receivable(db, number, amount, waiting=4405, rate=0.19):
    """Eine eigenständige Forderungszeile „10000 an Wartekonto“, wie ``_book_receivable`` sie schreibt."""
    return db.insert_booking('2026-01-01', amount, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, waiting),
                             tax_rate=rate, tax_amount=round(amount - amount / (1 + rate), 2), text=f'Rechnung {number}',
                             document_number=number, booking_type='entry', entry_form=FORM_TRANSFER)


def _pay(db, invoice_id, account_id, amount, date='2026-02-01'):
    """Eine Zahlung über ``amount`` zuordnen („Bank an Debitoren“ und „Wartekonto an Erlöskonto“); Returns die ID der Zuordnung."""
    bank = db.insert_booking(date, amount, account_id=account_id, booking_type='bank', recipient_client='Käufer AG')
    result = db.assign_invoice_payment(invoice_id, bank, amount)
    assert result.ok, result.message
    return result.payment_id


def _mixed(db, acct):
    """Rechnung über 172,50 € mit zwei Forderungszeilen unter EINER Beleg-Nr.: 119,00 € (Wartekonto 19 %) und 53,50 € (Wartekonto 7 %)."""
    invoice = _invoice(db, NUMBER, 172.50)
    return invoice, _receivable(db, NUMBER, 119.00), _receivable(db, NUMBER, 53.50, waiting=4345, rate=0.07)


# ── Mehrere Forderungszeilen ─────────────────────────────────────────────────

def test_a_part_payment_that_would_cover_the_smaller_row_resolves_neither(db, acct):
    """60,00 € auf 119,00 € + 53,50 €: Für sich genommen wäre die kleinere Zeile (53,50 €) „gedeckt“ – am Stand vor der neuen Regel
    wurde sie aufgelöst, obwohl die Rechnung erst zu einem Drittel bezahlt ist."""
    invoice, big, small = _mixed(db, acct)
    _pay(db, invoice, acct, 60.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, big), _status(db, small)) == (None, None)


def test_both_rows_resolve_together_once_the_payments_cover_the_sum(db, acct):
    invoice, big, small = _mixed(db, acct)
    _pay(db, invoice, acct, 60.00)
    assert db.link_bank_to_entries()['resolved'] == 0

    _pay(db, invoice, acct, 112.50, date='2026-02-02')                    # 60,00 + 112,50 = 172,50: die ganze Rechnung

    assert db.link_bank_to_entries()['resolved'] == 2                     # gezählt wird, was sich ändert: beide Zeilen
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')
    assert db.link_bank_to_entries()['resolved'] == 0                     # nichts mehr zu tun


def test_removing_a_payment_reopens_both_rows_together(db, acct):
    invoice, big, small = _mixed(db, acct)
    _pay(db, invoice, acct, 60.00)
    last = _pay(db, invoice, acct, 112.50, date='2026-02-02')
    db.link_bank_to_entries()
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')

    db.unassign_invoice_payment(last)                                      # die Summe ist nicht mehr gedeckt

    assert (_status(db, big), _status(db, small)) == (None, None)
    assert db.link_bank_to_entries()['resolved'] == 0                      # die verbleibenden 60,00 € decken die Summe nicht


def test_removing_a_payment_that_the_rest_still_covers_leaves_both_rows_resolved(db, acct):
    """Zwei Zahlungen, jede für sich so hoch wie die Summe (die zweite ein Satz „Bank an Debitoren“ ohne Zuordnung, Altbestand): Fällt
    die zugeordnete weg, deckt die andere die Summe weiter – die Zeilen bleiben aufgelöst."""
    invoice, big, small = _mixed(db, acct)
    first = _pay(db, invoice, acct, 172.50)
    legacy_bank = db.insert_booking('2026-02-03', 172.50, account_id=acct, booking_type='bank', recipient_client='Käufer AG')
    legacy = db.insert_booking('2026-02-03', 172.50, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                               parent_booking_id=legacy_bank, document_number=NUMBER, text='Zahlung (Altbestand, ohne Zuordnung)')
    db.link_bank_to_entries()
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')

    db.unassign_invoice_payment(first)

    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')
    assert db.get_booking_by_id(legacy) is not None


def test_a_row_resolved_earlier_still_counts_in_the_sum(db, acct):
    """Eine Zeile, die der Bestand schon als aufgelöst führt (nach der früheren Regel), zählt mit ihrem Betrag in der Summe: Nach
    119,00 € ist die Summe 172,50 € nicht gedeckt – die andere Zeile bleibt offen, erst die volle Zahlung löst sie."""
    invoice, big, small = _mixed(db, acct)
    con = sqlite3.connect(db.db_name)
    con.execute("UPDATE Bookings SET Status = 'resolved' WHERE ID = ?", (big,))
    con.commit()
    con.close()
    _pay(db, invoice, acct, 119.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, big), _status(db, small)) == ('resolved', None)

    _pay(db, invoice, acct, 53.50, date='2026-02-02')

    assert db.link_bank_to_entries()['resolved'] == 1                      # nur die Zeile, die sich ändert
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')


def test_rows_of_different_numbers_resolve_on_their_own(db, acct):
    paid_invoice, open_invoice = _invoice(db, 'R-PAID', 119.00), _invoice(db, 'R-OPEN', 119.00)
    paid_row, open_row = _receivable(db, 'R-PAID', 119.00), _receivable(db, 'R-OPEN', 119.00)
    _pay(db, paid_invoice, acct, 119.00)
    _pay(db, open_invoice, acct, 50.00, date='2026-02-02')

    assert db.link_bank_to_entries()['resolved'] == 1
    assert (_status(db, paid_row), _status(db, open_row)) == ('resolved', None)


def test_a_row_without_a_number_is_left_alone(db, acct):
    invoice, big, small = _mixed(db, acct)
    nameless, empty = _receivable(db, None, 10.00), _receivable(db, '', 10.00)
    _pay(db, invoice, acct, 172.50)

    assert db.link_bank_to_entries()['resolved'] == 2
    assert (_status(db, big), _status(db, small)) == ('resolved', 'resolved')
    assert (_status(db, nameless), _status(db, empty)) == (None, None)


# ── Genau eine Forderungszeile: wie heute ────────────────────────────────────

def test_a_single_row_resolves_with_the_covering_payment_and_reopens_with_its_removal(db, acct):
    invoice = _invoice(db, 'R-ONE', 119.00)
    row = db.ensure_receivable_booking(invoice)                            # eine Zeile, wie beim Versenden gebucht
    _pay(db, invoice, acct, 50.00)

    assert db.link_bank_to_entries()['resolved'] == 0                      # nach der Teilzahlung noch offen
    assert _status(db, row) is None

    last = _pay(db, invoice, acct, 69.00, date='2026-02-02')
    assert db.link_bank_to_entries()['resolved'] == 1
    assert _status(db, row) == 'resolved'
    assert db.link_bank_to_entries()['resolved'] == 0

    db.unassign_invoice_payment(last)
    assert _status(db, row) is None                                        # wieder offen


def test_a_single_row_stays_resolved_while_the_rest_still_covers_it(db, acct):
    invoice = _invoice(db, 'R-ONE2', 119.00)
    row = db.ensure_receivable_booking(invoice)
    _pay(db, invoice, acct, 119.00)
    db.link_bank_to_entries()
    assert _status(db, row) == 'resolved'

    other = _pay(db, _invoice(db, 'R-ONE3', 119.00), acct, 119.00, date='2026-02-03')
    db.unassign_invoice_payment(other)

    assert _status(db, row) == 'resolved'


def test_removing_a_payment_does_not_read_the_amount_of_an_open_row(db, acct):
    """Ohne aufgelöste Zeile gibt es nichts zurückzunehmen – dann wird auch kein Betrag gelesen: Ein Betrag, der keine Zahl ist (so schreibt ihn
    nur die SQL-Konsole), lässt das Entfernen einer Zahlung nicht scheitern. So war es vor der Summenregel, und so bleibt es."""
    invoice = _invoice(db, NUMBER, 119.00)
    row = _receivable(db, NUMBER, 119.00)
    payment = _pay(db, invoice, acct, 50.00)                               # Teilzahlung: die Forderung bleibt offen
    assert _status(db, row) is None
    con = sqlite3.connect(db.db_name)
    try:
        con.execute("UPDATE Bookings SET Amount = 'kein Betrag' WHERE ID = ?", (row,))
        con.commit()
    finally:
        con.close()

    assert db.unassign_invoice_payment(payment)                            # wirft nicht, die Zuordnung ist weg

    assert _raw(db, 'SELECT COUNT(*) FROM InvoicePayments WHERE InvoiceID = ?', invoice) == [(0,)]
    assert _status(db, row) is None


# ── Befund 1: Was eine Forderung ist, sagt ``receivable_condition`` (Schnittmenge mit „Debitoren als COA“) ────────────────

def _standalone(db, number, amount, counter, text='eigenständiger Satz'):
    """Eine eigenständige Zeile „10000 an ``counter``“ mit der Beleg-Nr., wie ein Bestand oder ein Import sie trägt."""
    return db.insert_booking('2026-01-01', amount, coa_id=_coa(db, 10000), counter_coa_id=_coa(db, counter), text=text,
                             document_number=number, booking_type='entry', entry_form=FORM_TRANSFER)


def test_a_row_10000_to_cash_is_no_receivable_and_does_not_count_in_the_sum(db, acct):
    """„10000 an Kasse“ hat ein Zahlungskonto auf einer Seite: keine Forderung. Mit +69,00 neben 119,00 und einer Teilzahlung von 50,00 ist nichts
    gedeckt – wie vorher."""
    invoice = _invoice(db, 'R-K', 119.00)
    receivable = _receivable(db, 'R-K', 119.00)
    cash = _standalone(db, 'R-K', 69.00, 1600)
    _pay(db, invoice, acct, 50.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, receivable), _status(db, cash)) == (None, None)


def test_a_row_10000_to_cash_with_a_negative_amount_does_not_make_a_partial_payment_look_like_a_full_one(db, acct):
    """Dieselbe Zeile mit −69,00: Mit ihr in der Summe wären es 119,00 − 69,00 = 50,00 – von den 50,00 € Teilzahlung „gedeckt“, und beide Zeilen würden
    aufgelöst. Sie ist keine Forderung (Zahlungskonto auf einer Seite) und zählt nicht."""
    invoice = _invoice(db, 'R-K', 119.00)
    receivable = _receivable(db, 'R-K', 119.00)
    cash = _standalone(db, 'R-K', -69.00, 1600)
    _pay(db, invoice, acct, 50.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, receivable), _status(db, cash)) == (None, None)


def test_a_row_10000_to_revenue_with_a_negative_amount_is_no_receivable_without_a_credit_note(db, acct):
    """„10000 an 4400“ mit −69,00 (10000 steht im Haben: eine Erlösminderung), ohne eine Gutschrift-Rechnung dieser Nummer: keine Forderung. Mit ihr in der
    Summe wären es 119,00 − 69,00 = 50,00, von der Teilzahlung „gedeckt“."""
    invoice = _invoice(db, 'R-K', 119.00)
    receivable = _receivable(db, 'R-K', 119.00)
    reduction = _standalone(db, 'R-K', -69.00, 4400)
    _pay(db, invoice, acct, 50.00)

    assert db.link_bank_to_entries()['resolved'] == 0
    assert (_status(db, receivable), _status(db, reduction)) == (None, None)


def test_such_rows_are_left_alone_when_the_real_receivable_is_paid_in_full(db, acct):
    invoice = _invoice(db, 'R-K', 119.00)
    receivable = _receivable(db, 'R-K', 119.00)
    cash = _standalone(db, 'R-K', 69.00, 1600)
    reduction = _standalone(db, 'R-K', -69.00, 4400)
    _pay(db, invoice, acct, 119.00)

    assert db.link_bank_to_entries()['resolved'] == 1                    # nur die Forderung
    assert (_status(db, receivable), _status(db, cash), _status(db, reduction)) == ('resolved', None, None)


def test_the_receivable_of_a_credit_note_stays_a_receivable(db, acct):
    """Die Forderung einer Gutschrift (Rechnung mit negativem Brutto: „10000 an Wartekonto“ mit negativem Betrag) bleibt eine Forderung – ihre
    Erstattung („1800 an 10000“ mit negativem Betrag unter einer Bankbewegung) löst sie auf."""
    credit = db.insert_invoice({
        'invoice_number': 'G-1', 'invoice_date': '2026-01-01', 'payment_due_date': '2099-12-31',
        'seller_name': 'Vera Verkäufer', 'seller_company': 'Verkäufer GmbH', 'buyer_name': 'Kim Käufer', 'buyer_company': 'Käufer AG',
        'tax_rate': 0.19, 'sum_net': -100.00, 'tax_amount': -19.00, 'sum_gross': -119.00, 'amount_due': -119.00, 'status': 'sent'})
    row = _receivable(db, 'G-1', -119.00)
    refund = db.insert_booking('2026-02-01', -119.00, account_id=acct, booking_type='bank', recipient_client='Käufer AG')
    db.insert_booking('2026-02-01', -119.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                      parent_booking_id=refund, document_number='G-1', entry_form='liquid_first', text='Erstattung')

    assert credit and db.link_bank_to_entries()['resolved'] == 1
    assert _status(db, row) == 'resolved'


# ── Befund 2: Debitoren sind alle Konten mit der Nummer 10000 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('on', ['standard', 'second'])
def test_a_receivable_on_either_debtor_account_is_resolved_by_its_payment(db, acct, on):
    """Zwei Kontenrahmen können ein Konto 10000 führen. Stufe 7 nahm bisher nur eines davon (das erste, das die Abfrage lieferte): Eine Forderung auf
    dem anderen, voll bezahlt über „Bank an Debitoren“ desselben Kontos, bliebe für immer offen. Jetzt zählen alle Konten mit der Nummer 10000."""
    standard = _coa(db, 10000)
    assert db.insert_chart_of_accounts(3, 10000, 'Debitoren (zweiter Rahmen)', 'Testkonto')
    second = _raw(db, 'SELECT ID FROM ChartOfAccounts WHERE Framework = 3 AND AccountNumber = 10000')[0][0]
    assert standard != second and len(_raw(db, 'SELECT ID FROM ChartOfAccounts WHERE AccountNumber = 10000')) == 2
    debtor = {'standard': standard, 'second': second}[on]
    _invoice(db, 'R-ZWEI', 119.00)
    row = db.insert_booking('2026-01-01', 119.00, coa_id=debtor, counter_coa_id=_coa(db, 4405), tax_rate=0.19, tax_amount=19.00,
                            text='Rechnung R-ZWEI', document_number='R-ZWEI', booking_type='entry', entry_form=FORM_TRANSFER)
    bank = db.insert_booking('2026-02-01', 119.00, account_id=acct, booking_type='bank', recipient_client='Käufer AG')
    db.insert_booking('2026-02-01', 119.00, coa_id=_coa(db, 1800), counter_coa_id=debtor, booking_type='entry', parent_booking_id=bank,
                      document_number='R-ZWEI', entry_form='liquid_first', text='Zahlung')

    assert db.link_bank_to_entries()['resolved'] == 1
    assert _status(db, row) == 'resolved'


def test_removing_a_payment_reopens_a_receivable_on_the_second_debtor_account_too(db, acct):
    """Das Entfernen einer Zuordnung (``_after_payment_removed``) liest dieselben Konten: Die Zahlung auf das zweite Debitorenkonto deckt die Forderung
    nicht mehr, wenn sie fehlt."""
    assert db.insert_chart_of_accounts(3, 10000, 'Debitoren (zweiter Rahmen)', 'Testkonto')
    second = _raw(db, 'SELECT ID FROM ChartOfAccounts WHERE Framework = 3 AND AccountNumber = 10000')[0][0]
    invoice = _invoice(db, 'R-ZWEI', 119.00)
    row = db.insert_booking('2026-01-01', 119.00, coa_id=second, counter_coa_id=_coa(db, 4405), tax_rate=0.19, tax_amount=19.00,
                            text='Rechnung R-ZWEI', document_number='R-ZWEI', booking_type='entry', entry_form=FORM_TRANSFER)
    payment = _pay(db, invoice, acct, 119.00)                                  # „Bank an Debitoren“ auf das Konto der Forderung (``_receivable_sides``)
    db.link_bank_to_entries()
    assert _status(db, row) == 'resolved'

    db.unassign_invoice_payment(payment)

    assert _status(db, row) is None


# ── Befund 3: Stufe 7 liest robust ────────────────────────────────────────────────────────────────────────────────────────

def _write(db, sql, *params):
    """Eine Anweisung an der App vorbei (wie die SQL-Konsole oder ein Bestand aus einer anderen Zeit)."""
    con = sqlite3.connect(db.db_name)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


@pytest.mark.parametrize('kind', ['receivable', 'payment'])
def test_a_document_number_that_is_not_valid_utf8_does_not_stop_the_matching(db, acct, kind):
    """Eine fremde Zeile mit einer Beleg-Nr., die kein gültiges UTF-8 ist (``CAST(X'FF' AS TEXT)``, so schreibt sie nur ein Eingriff von Hand): Stufe 7
    las die Nummern als Text und brach bei ihr ab – samt dem ganzen Abgleich. Jetzt wird sie als BLOB gelesen; die gedeckte Rechnung daneben wird
    aufgelöst."""
    invoice = _invoice(db, 'R-OK', 119.00)
    row = _receivable(db, 'R-OK', 119.00)
    _pay(db, invoice, acct, 119.00)
    if kind == 'receivable':
        foreign = _receivable(db, 'R-X', 10.00)                           # eine eigenständige Forderungszeile
    else:
        bank = db.insert_booking('2026-02-02', 10.00, account_id=acct, booking_type='bank', recipient_client='Käufer AG')
        foreign = db.insert_booking('2026-02-02', 10.00, coa_id=_coa(db, 1800), counter_coa_id=_coa(db, 10000), booking_type='entry',
                                    parent_booking_id=bank, entry_form='liquid_first', text='Zahlung', document_number='R-X')
    _write(db, "UPDATE Bookings SET DocumentNumber = CAST(X'FF' AS TEXT) WHERE ID = ?", foreign)

    result = db.link_bank_to_entries()

    assert result['resolved'] == 1 and result['errors'] == []
    assert _status(db, row) == 'resolved'


def test_payments_beyond_two_to_the_power_of_63_minor_units_do_not_stop_the_matching(db, acct):
    """Zwei Zahlungssätze je 2^62 Minor Units unter einer Bewegung: Die SQL-Summe bräche mit „integer overflow“ ab (und mit ihr der ganze Abgleich),
    Python rechnet mit ganzen Zahlen ohne Grenze. Die Forderung über 1,00 € gilt als gedeckt (eine Überzahlung deckt)."""
    _invoice(db, 'R-BIG', 1.00)
    row = _receivable(db, 'R-BIG', 1.00)
    bank = db.insert_booking('2026-02-01', 1.00, account_id=acct, booking_type='bank', recipient_client='Käufer AG')
    for _ in range(2):
        _write(db, "INSERT INTO Bookings (DateBooking, Amount, COA_ID, CounterCOA_ID, DocumentNumber, BookingType, ParentBooking_ID, EntryForm, "
                   "Currency, Text) VALUES ('2026-02-01', ?, ?, ?, 'R-BIG', 'entry', ?, 'liquid_first', 'EUR', 'Zahlung')",
               2 ** 62, _coa(db, 1800), _coa(db, 10000), bank)

    result = db.link_bank_to_entries()

    assert result['resolved'] == 1 and _status(db, row) == 'resolved'


@pytest.mark.parametrize('amount', ["'kein Betrag'", 'X\'00\'', '1190000.5'])
def test_a_forderung_without_a_number_as_amount_decides_nothing_and_stops_nothing(db, acct, amount):
    """Text, Blob oder ein Bruchteil von Minor Units sind kein Betrag, den die App schreibt: Stufe 7 löst die Nummer nicht auf – ohne Fehler, die
    Rechnung daneben wird aufgelöst. (Das ist ``receivables_covered``: kein Betrag, keine Antwort.)"""
    invoice = _invoice(db, 'R-OK', 119.00)
    ok = _receivable(db, 'R-OK', 119.00)
    _pay(db, invoice, acct, 119.00)
    broken_invoice = _invoice(db, 'R-BAD', 119.00)
    broken = _receivable(db, 'R-BAD', 119.00)
    _pay(db, broken_invoice, acct, 119.00, date='2026-02-02')
    _write(db, f'UPDATE Bookings SET Amount = {amount} WHERE ID = ?', broken)

    result = db.link_bank_to_entries()

    assert result['resolved'] == 1
    assert (_status(db, ok), _status(db, broken)) == ('resolved', None)


def test_removing_a_payment_leaves_resolved_rows_with_an_unreadable_amount_as_they_are(db, acct):
    """Das Entfernen einer Zuordnung nimmt eine aufgelöste Zeile nur zurück, wenn die Rechnung SICHER nicht mehr gedeckt ist; ein Betrag, der keine
    Zahl ist, entscheidet nichts – die Zeile bleibt, das Entfernen scheitert nicht."""
    invoice = _invoice(db, NUMBER, 119.00)
    row = _receivable(db, NUMBER, 119.00)
    payment = _pay(db, invoice, acct, 119.00)
    db.link_bank_to_entries()
    assert _status(db, row) == 'resolved'
    _write(db, "UPDATE Bookings SET Amount = 'kein Betrag' WHERE ID = ?", row)

    assert db.unassign_invoice_payment(payment)

    assert _status(db, row) == 'resolved'


def test_the_connection_is_closed_even_when_the_matching_fails(db, monkeypatch):
    """Die Verbindung von ``link_bank_to_entries`` schloss sich nur am Ende des Laufs; ein Fehler mittendrin ließ sie offen (und die Datenbank
    gesperrt). Jetzt läuft der Lauf in einer Hülle: Rollback, Ausnahme weiterreichen, Verbindung schließen."""
    opened = []
    real = db._get_connection

    def counting():
        connection = real()
        opened.append(connection)
        return connection

    def boom(conn, cursor):
        raise RuntimeError('mitten im Lauf')

    monkeypatch.setattr(db, '_get_connection', counting)
    monkeypatch.setattr(db, '_link_bank_to_entries_in', boom)

    with pytest.raises(RuntimeError, match='mitten im Lauf'):
        db.link_bank_to_entries()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):                          # „Cannot operate on a closed database“
        opened[0].execute('SELECT 1')


def test_the_connection_is_closed_after_a_normal_run_too_and_the_result_is_written(db, acct):
    invoice = _invoice(db, NUMBER, 119.00)
    row = _receivable(db, NUMBER, 119.00)
    _pay(db, invoice, acct, 119.00)

    assert db.link_bank_to_entries()['resolved'] == 1

    assert _status(db, row) == 'resolved'                                  # festgeschrieben


# ── ``receivables_covered`` (rein) ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('receivable, settled, expected', [
    ([1190000], [1190000], True),
    ([1190000], [500000], False),
    ([1190000, 535000], [1725000], True),                                  # die SUMME der Forderungszeilen
    ([1190000, 535000], [1190000], False),                                 # eine Zeile für sich wäre gedeckt – die Summe nicht
    ([1190000], [500000, 690000], True),                                   # die Summe der Zahlungen
    ([1190000], [2000000], True),                                          # eine Überzahlung deckt
    ([1190000], [1189950], True),                                          # Rest unter einem Cent
    ([1190000], [1189000], False),
    ([-1190000], [-1190000], True),                                        # Gutschrift
    ([1190000], [], False),                                                # keine Zahlung
    ([], [1190000], False),                                                # keine Forderung
    ([0], [0], False),                                                     # ein Betrag von 0 deckt nie
    ([1190000.0], [1190000], True),                                        # eine Kommazahl, die eine ganze Zahl ist
    ([1190000], [None, 1190000], True),                                    # NULL zählt 0
    ([None], [1190000], False),
    ([2 ** 62], [2 ** 62, 2 ** 62], True),                                 # Python-Ganzzahlen laufen nie über
    ([10 ** 40], [10 ** 40], True),
])
def test_receivables_covered_sums_in_python_and_compares_like_is_covered(receivable, settled, expected):
    from db.matching import receivables_covered
    assert receivables_covered(receivable, settled) is expected


@pytest.mark.parametrize('bad', ['kein Betrag', b'\x00', 1190000.5, float('inf'), float('nan'), True, False, '1190000', object()])
def test_receivables_covered_has_no_answer_for_something_that_is_no_amount(bad):
    from db.matching import receivables_covered
    assert receivables_covered([bad], [1190000]) is None
    assert receivables_covered([1190000], [bad]) is None
    assert receivables_covered([1190000, bad], [1190000]) is None
