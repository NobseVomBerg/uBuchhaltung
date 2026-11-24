# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""Die Buchungsmaske speichert nur ein Buchungsdatum, das ein Kalendertag ist (2.9.1).

Der Fehlfall: ``handle_add_transaction`` übernahm das Feld ``date`` ungeprüft. Der Browser verhindert über das Datumsfeld einen
anderen Wert, der Server tat es nicht – eine Anfrage mit ``kaputt``, einem leeren Feld, ``2026-02-30`` oder ``30.02.2026`` legte
eine Bankbewegung an, die in jeder Auswertung fehlte (die Summen- und Saldenliste nannte sie nur als Hinweis). Jetzt lehnt der
Server ab, bevor etwas geschrieben wird – beim Anlegen wie beim Ändern, für das Steuerdatum ebenso (leer bleibt erlaubt).

Namen und Beträge sind erfunden.
"""
import pytest

from server.handlers import BOOKING_DATE_INVALID, TAX_DATE_INVALID, handle_add_transaction
from tests.test_invoice_payment_assignment import _account, _bank

BAD_DATES = ['kaputt', '', '2026-02-30', '30.02.2026', '2026-2-3', '20260203', ' 2026-02-03', '2026-02-03T10:00', '0000-01-01']


def _form(account, date='2026-03-05', **over):
    form = {'transaction_id': ['0'], 'date': [date], 'recipient': ['Beispiel Lieferant'], 'text': ['Probe'],
            'amount': ['-10.00'], 'currency': ['EUR'], 'account': [str(account)]}
    form.update({key: [value] for key, value in over.items()})
    return form


def _count(db):
    conn = db._get_connection()
    try:
        return conn.execute('SELECT COUNT(*) FROM Bookings').fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def acct(tmp_db):
    return _account(tmp_db)


@pytest.mark.parametrize('date', BAD_DATES)
def test_a_new_booking_with_an_unreadable_date_is_refused_and_nothing_is_stored(tmp_db, acct, date):
    assert handle_add_transaction(tmp_db, _form(acct, date)) == (400, BOOKING_DATE_INVALID)
    assert _count(tmp_db) == 0


def test_a_new_booking_without_the_date_field_is_refused(tmp_db, acct):
    form = _form(acct)
    del form['date']
    assert handle_add_transaction(tmp_db, form) == (400, BOOKING_DATE_INVALID)
    assert _count(tmp_db) == 0


@pytest.mark.parametrize('date_tax', ['kaputt', '2026-02-30', '31.03.2026'])
def test_an_unreadable_tax_date_is_refused(tmp_db, acct, date_tax):
    assert handle_add_transaction(tmp_db, _form(acct, date_tax=date_tax)) == (400, TAX_DATE_INVALID)
    assert _count(tmp_db) == 0


def test_a_calendar_day_is_stored_with_and_without_a_tax_date(tmp_db, acct):
    assert handle_add_transaction(tmp_db, _form(acct))[0] == 303
    assert handle_add_transaction(tmp_db, _form(acct, '2024-02-29', date_tax='2024-03-01'))[0] == 303
    assert handle_add_transaction(tmp_db, _form(acct, date_tax=''))[0] == 303                  # leer heißt: kein Steuerdatum
    conn = tmp_db._get_connection()
    try:
        rows = conn.execute('SELECT DateBooking, DateTax FROM Bookings ORDER BY ID').fetchall()
    finally:
        conn.close()
    assert rows == [('2026-03-05', None), ('2024-02-29', '2024-03-01'), ('2026-03-05', None)]


@pytest.mark.parametrize('date', ['kaputt', '', '2026-02-30'])
def test_changing_a_booking_to_an_unreadable_date_is_refused_and_it_stays_as_it_was(tmp_db, acct, date):
    bank = _bank(tmp_db, acct, -10.00, date='2026-03-05', text='Probe')
    before = tmp_db.get_booking_by_id(bank)
    assert handle_add_transaction(tmp_db, _form(acct, date, transaction_id=str(bank))) == (400, BOOKING_DATE_INVALID)
    assert tmp_db.get_booking_by_id(bank) == before
