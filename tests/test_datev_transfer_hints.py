# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""DATEV-Export: die Hinweise 2 und 3 im Einzelnen – die letzte Fassung der Übertrags-Erkennung (2.3.4).

Die Zählregel steht in test_datev_transfer_duplicates.py, die Hinweise und die Prüfsumme in test_datev_unbooked.py. Hier die Fälle
aus der Nachprüfung der vierten Fassung, jeder mit den Zahlen der Prüfung – über die Datenbank und den echten Handler:

- Hinweis 2 je Abschnitt gezählt, nicht je Satz: ein Dauerauftrag über 100 Wochen, monatlich exportiert, braucht keinen Hinweis; nur
  Überträge, die eine Zeitraumgrenze wirklich überqueren, werden genannt – in dem Stapel, aus dem ihr Eingangs-Satz wegfällt, und (mit
  anderen Worten) in dem, der den Ausgangs-Satz trägt: War der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, steht
  der Übertrag dort ebenfalls
- anders kontierte Bewegungen (eine Zahlung unter dem Ausgang gleicher Höhe): Sie sind verbraucht und lassen die Gegenseite als
  möglicherweise doppelt nennen
- der Auslöser im Umfeld: die Bewegung, die das Zusammenlegen sperrt, mit Datum und Betrag
- eigenständige Spiegel ohne bekannte Lesart neben einer kontierten Bankbewegung
- Datums- und Betragsformen: nur ``JJJJ-MM-TT`` und ganze Zahlen nehmen teil, nichts bricht mit Python-Text ab
- die bekannten Grenzen, jede als benannter Test: Sammelüberweisung mit anderer Stückelung, zwei Konten mit derselben SKR-Nummer, eine
  weit entfernte Lücke in einer langen Reihe

Alle Werte sind erfundene Testdaten.
"""
import csv
import sqlite3
import time
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from server import handlers
from tests.datev_helpers import confirmed_export, datev_post, is_confirm_redirect, message_of

PERIOD = ('2026-03-01', '2026-03-31')
SUFFIX = ' Bitte bestätigen, um trotzdem zu exportieren.'
_SPALTE_BETRAG, _SPALTE_SH, _SPALTE_KONTO, _SPALTE_GEGENKONTO = 0, 1, 6, 7

DOUBTFUL_CAUSE = ('im Umfeld ist nicht jede Bankbewegung kontiert, die beiden Seiten liegen weit auseinander, oder es gibt zusätzlich '
                  'einen eigenständigen Umbuchungssatz')
DOUBTFUL_ONE = '1 Übertrag zwischen eigenen Konten steht möglicherweise doppelt im Stapel – ' + DOUBTFUL_CAUSE + ': '
DOUBTFUL_MANY = ' Überträge zwischen eigenen Konten stehen möglicherweise doppelt im Stapel – ' + DOUBTFUL_CAUSE + ': '
END_ONE = '. Bitte die Bankbewegungen dieses Tages prüfen.'
END_MANY = '. Bitte die Bankbewegungen dieser Tage prüfen.'
TRIGGER = ' (Auslöser im Umfeld: {} {} EUR)'

INCOMING_ONE = ('1 Übertrag zwischen eigenen Konten steht mit seiner Ausgangsseite außerhalb des Zeitraums und deshalb nicht in diesem '
                'Stapel – {}. Er gehört in den Stapel des Zeitraums seiner Ausgangsseite.')
OUTGOING_ONE = ('1 Übertrag steht mit seiner Ausgangsseite in diesem Stapel, seine Eingangsseite liegt außerhalb des Zeitraums – wurde '
                'der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, steht er dort ebenfalls: {}.')
OUTGOING_MANY = ('{} Überträge stehen mit ihrer Ausgangsseite in diesem Stapel, ihre Eingangsseite liegt außerhalb des Zeitraums – wurde '
                 'der andere Zeitraum schon exportiert, bevor beide Seiten kontiert waren, stehen sie dort ebenfalls: {}.')


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


@pytest.fixture
def world(tmp_db):
    """Zwei eigene Bankkonten (SKR 1800 und 1810) neben der Standard-Kasse (SKR 1600, ``cash``)."""
    def account(name, skr):
        tmp_db.insert_account(name, 'Owner', 'DE00HINTS', 'BIC', 'Bank', is_cash=0, skr_account=skr)
        return next(a[0] for a in tmp_db.fetch_accounts() if a[1] == name)
    cash = next(a[0] for a in tmp_db.fetch_accounts() if a[6] == 1)
    return SimpleNamespace(db=tmp_db, a=account('Hints-Eins', 1800), b=account('Hints-Zwei', 1810), cash=cash)


def _bank(world, account, day_text, amount):
    return world.db.insert_booking(day_text, Decimal(amount), account_id=account, booking_type='bank')


def _book(world, parent, day_text, amount, coa, counter, form=None):
    """Ein Satz unter einer Bankbewegung (``parent``) oder – ohne – eigenständig. Ohne ``form`` leitet ihn die Datenbank aus den
    Konten ab, wie die Buchungsmaske: Ein Spiegel zwischen zwei eigenen Konten wird ``mirror_counter``."""
    return world.db.insert_booking(day_text, Decimal(amount), coa_id=_coa(world.db, coa), counter_coa_id=_coa(world.db, counter),
                                   booking_type='entry', parent_booking_id=parent, entry_form=form)


def _out_booked(world, day_text, amount='100.00'):
    """Ausgang auf A mit dem Spiegel „1810 / 1800“. Gibt (Bankbewegung, Satz) zurück."""
    bank = _bank(world, world.a, day_text, f'-{amount}')
    return bank, _book(world, bank, day_text, f'-{amount}', 1810, 1800)


def _in_booked(world, day_text, amount='100.00'):
    """Eingang auf B mit dem Spiegel „1800 / 1810“. Gibt (Bankbewegung, Satz) zurück."""
    bank = _bank(world, world.b, day_text, amount)
    return bank, _book(world, bank, day_text, amount, 1800, 1810)


def _expense(world, day_text, amount='-10.00'):
    """Ein eigenständiger Aufwandssatz – damit ein Stapel nicht leer ist."""
    return _book(world, None, day_text, amount, 6815, 1800)


def _execute(db, sql, params=()):
    con = sqlite3.connect(db.db_name)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _handler(world, date_from, date_to, **more):
    return handlers.handle_datev_export(world.db, datev_post(date_from, date_to, **more))


def _rows_of(result):
    assert isinstance(result[0], bytes), result
    lines = [line for line in result[0].decode('cp1252').split('\r\n') if line][2:]
    return [next(csv.reader([line], delimiter=';', quotechar='"')) for line in lines]


def _stack(world, period=PERIOD):
    """Der Stapel nach ausdrücklicher Bestätigung: die Felder je Zeile, ohne Vorlauf und Überschrift."""
    return _rows_of(confirmed_export(world.db, *period))


def _balance(rows, number):
    """Saldo eines Kontos im Stapel in Cent (Soll +): Konto und Gegenkonto, S/H wie in der Datei."""
    total = 0
    for fields in rows:
        value = int(Decimal(fields[_SPALTE_BETRAG].replace(',', '.')) * 100)
        if fields[_SPALTE_KONTO] == str(number):
            total += value if fields[_SPALTE_SH] == 'S' else -value
        if fields[_SPALTE_GEGENKONTO] == str(number):
            total += -value if fields[_SPALTE_SH] == 'S' else value
    return total


def _first_of_month(year, month):
    return date(year, month, 1)


def _last_of_month(year, month):
    return (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))


def _weekly_chain(world, weeks=100, start=date(2025, 1, 6), lag=0, amount=500_000, skip_out_entry=None):
    """Ein wöchentlicher Dauerauftrag A → B, beidseitig kontiert, über ``weeks`` Wochen (Ausgang montags, Eingang ``lag`` Tage danach)
    – direkt per SQL. Gibt ``[(Ausgangstag, Eingangstag, Satz Ausgang, Satz Eingang)]`` zurück."""
    db = world.db
    coa_a, coa_b = _coa(db, 1800), _coa(db, 1810)
    weeks_list = []
    con = sqlite3.connect(db.db_name)
    try:
        next_id = con.execute('SELECT COALESCE(MAX(ID), 0) FROM Bookings').fetchone()[0] + 1
        rows = []
        for week in range(weeks):
            out_day = start + timedelta(weeks=week)
            in_day = out_day + timedelta(days=lag)
            first = next_id + 4 * week
            rows += [(first, out_day.isoformat(), world.a, None, None, -amount, 'bank', None, None),
                     (first + 1, out_day.isoformat(), None, coa_b, coa_a, -amount, 'entry', first, 'mirror_counter'),
                     (first + 2, in_day.isoformat(), world.b, None, None, amount, 'bank', None, None),
                     (first + 3, in_day.isoformat(), None, coa_a, coa_b, amount, 'entry', first + 2, 'mirror_counter')]
            weeks_list.append((out_day, in_day, first + 1, first + 3))
        con.executemany('INSERT INTO Bookings (ID, DateBooking, Account_ID, COA_ID, CounterCOA_ID, Amount, BookingType, '
                        'ParentBooking_ID, EntryForm, Currency) VALUES (?,?,?,?,?,?,?,?,?,"EUR")', rows)
        con.commit()
    finally:
        con.close()
    return weeks_list


# ═════════════════════════════════════════════════════════════════════════════
# A. Hinweis 2 je Abschnitt gezählt – ein Dauerauftrag, monatlich exportiert
# ═════════════════════════════════════════════════════════════════════════════

def test_a_hundred_weekly_transfers_booked_on_both_sides_need_no_hint_in_any_month(world):
    """Der Dauerauftrag über 100 Wochen (Ausgang und Eingang am selben Tag, beide kontiert) bildet einen einzigen Abschnitt: Jeder
    Monatsstapel hat genauso viele Ausgangs-Sätze wie weggelassene Eingangs-Sätze – kein Hinweis, auch nicht am Monatsrand."""
    _weekly_chain(world)
    _expense(world, '2026-03-10')
    for year, month in [(2025, number) for number in range(1, 13)] + [(2026, number) for number in range(1, 13)]:
        check = world.db.check_transfers(_first_of_month(year, month).isoformat(), _last_of_month(year, month).isoformat())
        assert (check.unbooked, check.elsewhere, check.elsewhere_out, check.doubtful, check.trigger) == ([], [], [], [], None), (year, month)
        assert len(check.omitted) == 100
    rows = _rows_of(_handler(world, *PERIOD))                      # März 2026: fünf Montage und der Aufwand, ohne Bestätigung
    assert sorted(fields[_SPALTE_BETRAG] for fields in rows) == ['10,00'] + ['50,00'] * 5


def test_a_weekly_standing_order_with_the_incoming_side_two_days_later_names_only_the_transfers_across_a_month_boundary(world):
    """Dieselbe Reihe, der Eingang zwei Tage nach dem Ausgang (Wertstellung): Genannt werden je Monat nur die Überträge, die die Grenze
    wirklich überqueren – im Stapel des Eingangs-Monats (Ausgang im Vormonat) als „außerhalb des Zeitraums“, im Stapel des
    Ausgangs-Monats (Eingang im Folgemonat) als „Eingangsseite außerhalb“."""
    chain = _weekly_chain(world, lag=2)
    came_in_total = went_out_total = 0
    for year, month in [(2025, number) for number in range(1, 13)] + [(2026, number) for number in range(1, 13)]:
        first, last = _first_of_month(year, month), _last_of_month(year, month)
        check = world.db.check_transfers(first.isoformat(), last.isoformat())
        came_in = sorted(in_day for out_day, in_day, _o, _i in chain if first <= in_day <= last and out_day < first)
        went_out = sorted(out_day for out_day, in_day, _o, _i in chain if first <= out_day <= last and in_day > last)
        assert [entry[1] for entry in check.elsewhere] == [day.isoformat() for day in came_in], (year, month)
        assert [entry[1] for entry in check.elsewhere_out] == [day.isoformat() for day in went_out], (year, month)
        assert check.doubtful == [] and check.unbooked == []
        came_in_total += len(came_in)
        went_out_total += len(went_out)
    assert came_in_total == went_out_total > 0                      # jeder überquerende Übertrag steht in zwei Stapeln, je einmal


def test_the_crossing_transfer_of_a_weekly_chain_is_named_in_both_stacks_in_each_words(world):
    """Montag 30.3.2026 → Mittwoch 1.4.2026 (Ausgang und Eingang je mit Satz): der März-Stapel trägt den Ausgangs-Satz und sagt, dass
    die Eingangsseite im April liegt; der April-Stapel lässt den Eingangs-Satz weg und sagt, dass der Ausgang im März steht."""
    _weekly_chain(world, lag=2)
    _expense(world, '2026-03-10')
    _expense(world, '2026-04-10')
    march = _handler(world, '2026-03-01', '2026-03-31')
    assert is_confirm_redirect(march)
    assert message_of(march) == 'Nicht exportiert: ' + OUTGOING_ONE.format('2026-03-30 50,00 EUR') + SUFFIX
    april = _handler(world, '2026-04-01', '2026-04-30')
    assert is_confirm_redirect(april)
    assert message_of(april) == 'Nicht exportiert: ' + INCOMING_ONE.format('2026-04-01 50,00 EUR') + SUFFIX
    assert len(_stack(world, ('2026-03-01', '2026-03-31'))) == 5 + 1                  # fünf Ausgangs-Sätze (2., 9., 16., 23., 30.) und der Aufwand
    assert len(_stack(world, ('2026-04-01', '2026-04-30'))) == 4 + 1                  # vier Ausgangs-Sätze (6., 13., 20., 27.) und der Aufwand


# ═════════════════════════════════════════════════════════════════════════════
# B. Hinweis 2, zweiter Satz: die Ausgangsseite im Stapel, die Eingangsseite außerhalb
# ═════════════════════════════════════════════════════════════════════════════

def test_two_outgoing_entries_with_their_incoming_side_in_the_next_period_are_named_in_the_plural(world):
    for out_day, in_day in (('2026-03-30', '2026-04-01'), ('2026-03-31', '2026-04-02')):
        _out_booked(world, out_day)
        _in_booked(world, in_day)
    _expense(world, '2026-03-10')
    result = _handler(world, *PERIOD)
    assert is_confirm_redirect(result)
    assert message_of(result) == 'Nicht exportiert: ' + OUTGOING_MANY.format(2, '2026-03-30 100,00 EUR; 2026-03-31 100,00 EUR') + SUFFIX


def test_both_sentences_of_the_second_hint_come_in_one_message_first_the_incoming_then_the_outgoing_side(world):
    """April: Der Übertrag über 700,00 (Ausgang 31.3., Eingang 1.4.) lässt seinen Eingangs-Satz weg; der über 800,00 (Ausgang 30.4.,
    Eingang 2.5.) trägt hier den Ausgangs-Satz – zwei Sätze, ein Hinweis."""
    _out_booked(world, '2026-03-31', '700.00')
    _in_booked(world, '2026-04-01', '700.00')
    _out_booked(world, '2026-04-30', '800.00')
    _in_booked(world, '2026-05-02', '800.00')
    _expense(world, '2026-04-10')
    result = _handler(world, '2026-04-01', '2026-04-30')
    assert message_of(result) == ('Nicht exportiert: ' + INCOMING_ONE.format('2026-04-01 700,00 EUR') + ' '
                                  + OUTGOING_ONE.format('2026-04-30 800,00 EUR') + SUFFIX)


def test_the_outgoing_side_booked_after_the_export_of_the_incoming_side_asks_in_the_new_year(world):
    """Der Eingang in der Kasse am 30.12. (kontiert, im Stapel 2025), die Abbuchung vom Bankkonto erst am 2.1. und noch ohne Satz
    (gedeckt, kein Hinweis). Kontiert der Nutzer den Ausgang später, wandert der Übertrag in den Stapel 2026 – und der sagt, dass
    die Eingangsseite im Vorjahr liegt: War 2025 schon exportiert, steht der Übertrag dort ebenfalls."""
    cash_in = _bank(world, world.cash, '2025-12-30', '800.00')
    _book(world, cash_in, '2025-12-30', '800.00', 1800, 1600)
    out = _bank(world, world.a, '2026-01-02', '-800.00')
    _expense(world, '2025-12-15')
    _expense(world, '2026-01-15')
    assert len(_rows_of(_handler(world, '2025-01-01', '2025-12-31'))) == 2                       # Aufwand und Kassen-Satz, ohne Hinweis
    _book(world, out, '2026-01-02', '-800.00', 1600, 1800)
    year_2026 = _handler(world, '2026-01-01', '2026-12-31')
    assert is_confirm_redirect(year_2026)
    assert message_of(year_2026) == 'Nicht exportiert: ' + OUTGOING_ONE.format('2026-01-02 800,00 EUR') + SUFFIX
    year_2025 = _handler(world, '2025-01-01', '2025-12-31')
    assert message_of(year_2025) == 'Nicht exportiert: ' + INCOMING_ONE.format('2025-12-30 800,00 EUR') + SUFFIX


def test_the_year_boundary_between_two_bank_accounts_asks_in_both_years_once_the_outgoing_side_is_booked(world):
    """Wie oben mit zwei Bankkonten: Eingang auf B am 30.12. (kontiert), Ausgang auf A am 2.1. (noch ohne Satz) – 2025 braucht keinen
    Hinweis; mit dem Satz auf dem Ausgang fragen beide Jahre."""
    _in_booked(world, '2025-12-30', '800.00')
    out = _bank(world, world.a, '2026-01-02', '-800.00')
    _expense(world, '2025-12-15')
    _expense(world, '2026-01-15')
    assert len(_rows_of(_handler(world, '2025-01-01', '2025-12-31'))) == 2
    _book(world, out, '2026-01-02', '-800.00', 1810, 1800)
    assert is_confirm_redirect(_handler(world, '2026-01-01', '2026-12-31'))
    assert 'Ausgangsseite außerhalb des Zeitraums' in message_of(_handler(world, '2025-01-01', '2025-12-31'))


# ═════════════════════════════════════════════════════════════════════════════
# C. Alternierend und doppelt kontiert (p12): die Fälle der Nachprüfung
# ═════════════════════════════════════════════════════════════════════════════

def test_alternating_transfers_each_booked_on_one_side_keep_both_entries_and_report_both_free_movements(world):
    """Ausgang am 2.3. kontiert, Eingang am 2.3. frei; Ausgang am 9.3. frei, Eingang am 9.3. kontiert: Beide Sätze bleiben, beide freien
    Bewegungen und der Eingangs-Satz vom 9.3. (mit dem Ausgang am 9.3. als Auslöser) stehen im Hinweis – nichts geht verloren."""
    _out_booked(world, '2026-03-02', '500.00')
    free_in = _bank(world, world.b, '2026-03-02', '500.00')
    free_out = _bank(world, world.a, '2026-03-09', '-500.00')
    _in_booked(world, '2026-03-09', '500.00')
    check = world.db.check_transfers(*PERIOD)
    assert [entry[0] for entry in check.unbooked] == [free_in, free_out] and check.omitted == set()
    assert [(entry[1], entry[2]) for entry in check.doubtful] == [('2026-03-09', Decimal('500'))] and check.trigger == ('2026-03-09', Decimal('500'))
    result = _handler(world, *PERIOD)
    assert message_of(result) == (
        'Nicht exportiert: 2 Bankbewegungen im Zeitraum haben keinen Buchungssatz und stehen nicht im Stapel '
        '(Eingänge 500,00 EUR, Ausgänge 500,00 EUR) – 2026-03-02 500,00 EUR; 2026-03-09 -500,00 EUR. '
        + DOUBTFUL_ONE + '2026-03-09 500,00 EUR' + END_ONE + TRIGGER.format('2026-03-09', '500,00') + SUFFIX)
    rows = _stack(world)
    assert len(rows) == 2 and _balance(rows, 1800) == -100_000 and _balance(rows, 1810) == 100_000      # = Summe der Bankbewegungen: je 1.000,00


def test_a_supplier_payment_of_the_same_amount_next_to_a_transfer_booked_on_both_sides_is_reported_and_names_its_trigger(world):
    """Der Übertrag über 500,00 beidseitig kontiert (zwei Sätze), dazu am 4.3. eine Lieferantenzahlung gleicher Höhe ohne Satz auf A:
    Sie wird gemeldet (Hinweis 1), nichts fällt weg, und der Eingangs-Satz steht im Hinweis 3 mit der Zahlung als Auslöser."""
    _out_booked(world, '2026-03-02', '500.00')
    _in_booked(world, '2026-03-02', '500.00')
    _bank(world, world.a, '2026-03-04', '-500.00')
    result = _handler(world, *PERIOD)
    assert message_of(result) == (
        'Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
        '(Eingänge 0,00 EUR, Ausgänge 500,00 EUR) – 2026-03-04 -500,00 EUR. '
        + DOUBTFUL_ONE + '2026-03-02 500,00 EUR' + END_ONE + TRIGGER.format('2026-03-04', '500,00') + SUFFIX)
    assert len(_stack(world)) == 2


def test_booking_the_outgoing_side_of_a_transfer_on_a_month_boundary_changes_the_hints_of_both_months(world):
    """Der Ausgang am 31.3. ohne Satz (gedeckt), der Eingang am 1.4. kontiert: Der März-Stapel hat keinen Hinweis, der April-Stapel den
    Eingangs-Satz. Kontiert der Nutzer den Ausgang, kommt der Hinweis im März (Eingangsseite im April) und im April (Ausgang im März)."""
    out = _bank(world, world.a, '2026-03-31', '-700.00')
    _in_booked(world, '2026-04-01', '700.00')
    _expense(world, '2026-03-15')
    _expense(world, '2026-04-15')
    assert len(_rows_of(_handler(world, '2026-03-01', '2026-03-31'))) == 1
    assert len(_rows_of(_handler(world, '2026-04-01', '2026-04-30'))) == 2
    _book(world, out, '2026-03-31', '-700.00', 1810, 1800)
    assert message_of(_handler(world, '2026-03-01', '2026-03-31')) == 'Nicht exportiert: ' + OUTGOING_ONE.format('2026-03-31 700,00 EUR') + SUFFIX
    assert message_of(_handler(world, '2026-04-01', '2026-04-30')) == 'Nicht exportiert: ' + INCOMING_ONE.format('2026-04-01 700,00 EUR') + SUFFIX


# ═════════════════════════════════════════════════════════════════════════════
# D. Anders kontierte Bewegungen (p16 a): verbraucht, und sie lassen die Gegenseite nennen
# ═════════════════════════════════════════════════════════════════════════════

def _transfer_with_the_outgoing_side_booked_as_a_payment(world):
    """Ausgang am 2.3. mit einem Satz in anderer Form (``purpose_first``: ein Zahlungssatz, kein Spiegel – Konto B war damals noch kein
    eigenes), der Eingang am 3.3. als Spiegel, am 5.3. eine Lieferantenzahlung gleicher Höhe ohne Satz auf A."""
    out = _bank(world, world.a, '2026-03-02', '-300.00')
    _book(world, out, '2026-03-02', '-300.00', 1810, 1800, form='purpose_first')
    _in_bank, in_row = _in_booked(world, '2026-03-03', '300.00')
    supplier = _bank(world, world.a, '2026-03-05', '-300.00')
    return out, in_row, supplier


def test_a_supplier_payment_next_to_a_transfer_whose_outgoing_side_is_booked_otherwise_is_reported_and_the_incoming_entry_named(world):
    """Ohne die Regel deckte der Eingangs-Satz die Lieferantenzahlung still – obwohl sein Ausgang schon (anders) kontiert ist. Jetzt ist
    der Ausgang verbraucht: Die Zahlung wird gemeldet, der Eingangs-Satz als möglicherweise doppelt genannt (Auslöser: der Ausgang)."""
    _out, in_row, supplier = _transfer_with_the_outgoing_side_booked_as_a_payment(world)
    check = world.db.check_transfers(*PERIOD)
    assert [entry[0] for entry in check.unbooked] == [supplier] and check.omitted == set()
    assert [entry[0] for entry in check.doubtful] == [in_row] and check.trigger == ('2026-03-02', Decimal('300'))
    result = _handler(world, *PERIOD)
    assert message_of(result) == (
        'Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
        '(Eingänge 0,00 EUR, Ausgänge 300,00 EUR) – 2026-03-05 -300,00 EUR. '
        + DOUBTFUL_ONE + '2026-03-03 300,00 EUR' + END_ONE + TRIGGER.format('2026-03-02', '300,00') + SUFFIX)
    assert len(_stack(world)) == 2                                                            # der Zahlungssatz und der Eingangs-Satz


def test_booking_the_supplier_payment_clears_the_hint_but_not_the_naming_of_the_incoming_entry(world):
    """Ist die Lieferantenzahlung kontiert, bleibt der Hinweis 3 (ein Ausgang gleicher Höhe, anders kontiert, im Umfeld des Eingangs-Satzes
    – die Rechnung kann nicht wissen, welcher Ausgang zum Eingang gehört) – aber die freie Bewegung ist weg."""
    _out, _in_row, supplier = _transfer_with_the_outgoing_side_booked_as_a_payment(world)
    _book(world, supplier, '2026-03-05', '-300.00', 6815, 1800)
    check = world.db.check_transfers(*PERIOD)
    assert check.unbooked == [] and len(check.doubtful) == 1 and check.trigger == ('2026-03-02', Decimal('300'))


def test_the_mirror_image_a_customer_payment_next_to_a_transfer_whose_incoming_side_is_booked_otherwise_is_reported(world):
    """Ausgang am 2.3. als Spiegel, der Eingang am 3.3. als Erlös gebucht (``purpose_first``, kein Spiegel), am 5.3. ein Zahlungseingang
    gleicher Höhe ohne Satz auf B: Gemeldet wird die Zahlung, der Ausgangs-Satz steht im Hinweis 3 (Auslöser: der Eingang)."""
    _out, out_row = _out_booked(world, '2026-03-02', '300.00')
    paid = _bank(world, world.b, '2026-03-03', '300.00')
    _book(world, paid, '2026-03-03', '300.00', 4400, 1810, form='purpose_first')
    customer = _bank(world, world.b, '2026-03-05', '300.00')
    check = world.db.check_transfers(*PERIOD)
    assert [entry[0] for entry in check.unbooked] == [customer]
    assert [entry[0] for entry in check.doubtful] == [out_row] and check.trigger == ('2026-03-03', Decimal('300'))
    assert check.omitted == set()


def test_a_payment_of_another_amount_or_beyond_the_neighbourhood_changes_nothing(world):
    """Dieselbe Lage, aber die Zahlung unter dem Ausgang trägt einen anderen Betrag, oder sie liegt 15 Tage vor dem Eingang: Die freie
    Bewegung ist wie bisher durch den Eingangs-Satz gedeckt, und nichts wird genannt."""
    out = _bank(world, world.a, '2026-03-02', '-299.00')
    _book(world, out, '2026-03-02', '-299.00', 1810, 1800, form='purpose_first')
    _in_booked(world, '2026-03-03', '300.00')
    _bank(world, world.a, '2026-03-05', '-300.00')
    check = world.db.check_transfers(*PERIOD)
    assert check.unbooked == [] and check.doubtful == [] and check.trigger is None

    other = _bank(world, world.a, '2026-04-01', '-300.00')
    _book(world, other, '2026-04-01', '-300.00', 6815, 1800)                          # 29 Tage nach dem Eingang am 3.3.
    assert world.db.check_transfers(*PERIOD).unbooked == []


# ═════════════════════════════════════════════════════════════════════════════
# E. Der Auslöser im Umfeld
# ═════════════════════════════════════════════════════════════════════════════

def test_a_far_gap_in_a_standing_order_blocks_the_whole_series_and_the_hint_names_the_gap(world):
    """Die bekannte Grenze: Fehlt in einer langen, lückenlosen Reihe (wöchentlich, 100 Wochen) an einer Stelle der Ausgangs-Satz – hier
    am 10.2.2025, ein Jahr vor dem März 2026 –, bildet die Reihe einen einzigen Abschnitt, und der Ausgang ohne Satz sperrt ihn ganz:
    Nichts fällt weg, im März stehen die fünf Eingangs-Sätze doppelt. Der Hinweis sagt wenigstens, WOHER es kommt: den Auslöser."""
    chain = _weekly_chain(world)
    _expense(world, '2026-03-10')
    _execute(world.db, 'DELETE FROM Bookings WHERE ID = ?', (chain[5][2],))                       # der Ausgangs-Satz der sechsten Woche
    assert chain[5][0] == date(2025, 2, 10)
    result = _handler(world, *PERIOD)
    assert message_of(result) == (
        'Nicht exportiert: 5 Überträge zwischen eigenen Konten stehen möglicherweise doppelt im Stapel – ' + DOUBTFUL_CAUSE + ': '
        '2026-03-02 50,00 EUR; 2026-03-09 50,00 EUR; 2026-03-16 50,00 EUR; 2026-03-23 50,00 EUR; 2026-03-30 50,00 EUR'
        + END_MANY + TRIGGER.format('2025-02-10', '50,00') + SUFFIX)
    rows = _stack(world)
    assert len(rows) == 11 and _balance(rows, 1810) == 50_000 and world.db.duplicate_transfer_rows() == set()      # die Grenze: doppelt, gemeldet


def test_the_series_merges_again_when_the_missing_entry_is_booked(world):
    chain = _weekly_chain(world)
    _expense(world, '2026-03-10')
    _execute(world.db, 'DELETE FROM Bookings WHERE ID = ?', (chain[5][2],))
    assert is_confirm_redirect(_handler(world, *PERIOD))
    _book(world, chain[5][2] - 1, chain[5][0].isoformat(), '-50.00', 1810, 1800)                  # die Bankbewegung davor: ihr Satz wird neu gebucht
    assert len(world.db.duplicate_transfer_rows()) == 100
    assert len(_rows_of(_handler(world, *PERIOD))) == 6


def test_the_trigger_is_a_standalone_mirror_when_it_is_the_oldest_blocker(world):
    """Ein Import-Spiegel („1810 an 1800“, ohne Bankbewegung) am 5.3. neben dem beidseitig kontierten Übertrag vom 2.3.: Er sperrt das
    Zusammenlegen und ist der Auslöser, wenn nichts Älteres im Umfeld liegt – ebenso wird er selbst genannt (Hinweis 3)."""
    _out_booked(world, '2026-03-02', '120.00')
    _in_booked(world, '2026-03-02', '120.00')
    _book(world, None, '2026-03-05', '120.00', 1810, 1800, form='mirror_coa')
    message = message_of(_handler(world, *PERIOD))
    assert message.startswith('Nicht exportiert: 2 Überträge zwischen eigenen Konten stehen möglicherweise doppelt im Stapel – ')
    assert message.endswith(END_MANY + TRIGGER.format('2026-03-05', '120,00') + SUFFIX)
    assert '2026-03-02 120,00 EUR; 2026-03-05 120,00 EUR' in message


def test_the_trigger_is_the_oldest_blocker_for_the_entries_of_the_period_not_of_the_whole_stock(world):
    """Im Januar sperrt ein Ausgang ohne Satz am 6.1. den Eingangs-Satz vom 12.1.; im März sperrt ein anderer am 4.3. Der Hinweis für den
    März nennt den vom 4.3. – nicht den älteren aus dem Januar, dessen Eingangs-Satz gar nicht im Stapel steht."""
    _out_booked(world, '2026-01-02')
    _in_booked(world, '2026-01-12')
    _bank(world, world.a, '2026-01-06', '-100.00')
    _out_booked(world, '2026-03-02')
    _in_booked(world, '2026-03-02')
    _bank(world, world.a, '2026-03-04', '-100.00')
    january = world.db.check_transfers('2026-01-01', '2026-01-31')
    march = world.db.check_transfers(*PERIOD)
    assert january.trigger == ('2026-01-06', Decimal('100')) and march.trigger == ('2026-03-04', Decimal('100'))
    assert message_of(_handler(world, *PERIOD)).endswith(END_ONE + TRIGGER.format('2026-03-04', '100,00') + SUFFIX)


def test_the_trigger_names_only_date_and_amount_never_text_or_recipient(world):
    out = world.db.insert_booking('2026-03-02', Decimal('-100.00'), account_id=world.a, booking_type='bank',
                                  text='GEHEIM-TEXT', recipient_client='GEHEIM-EMPFAENGER')
    _book(world, out, '2026-03-02', '-100.00', 1810, 1800)
    _in_booked(world, '2026-03-02', '100.00')
    _bank(world, world.a, '2026-03-04', '-100.00')
    location = _handler(world, *PERIOD)[1]
    assert 'GEHEIM' not in location and 'GEHEIM' not in message_of((303, location))
    assert quote(TRIGGER.format('2026-03-04', '100,00')[1:], safe='') in location


# ═════════════════════════════════════════════════════════════════════════════
# F. Eigenständige Spiegel ohne bekannte Lesart neben einer kontierten Bankbewegung (p14, Fall 2)
# ═════════════════════════════════════════════════════════════════════════════

def _legacy_next_to_a_booked_transfer(world, form='mirror_legacy', legacy_day='2026-03-02', booked_day='2026-03-02'):
    """Ein ``mirror_legacy``-Spiegel „1800 an 1810“ über 100,00 (Import aus 2.0.x) und ein von Hand kontierter Übertrag gleicher Höhe:
    der Ausgang mit Spiegel, der Eingang noch ohne Satz."""
    legacy = _book(world, None, legacy_day, '100.00', 1800, 1810, form=form)
    out, out_row = _out_booked(world, booked_day, '100.00')
    free_in = _bank(world, world.b, booked_day, '100.00')
    return legacy, out, out_row, free_in


def test_a_legacy_mirror_next_to_a_booked_transfer_of_the_same_amount_is_named_and_nothing_is_left_out(world):
    legacy, _out, _out_row, _free_in = _legacy_next_to_a_booked_transfer(world)
    check = world.db.check_transfers(*PERIOD)
    assert [entry[0] for entry in check.doubtful] == [legacy] and check.trigger is None and check.omitted == set()
    assert check.unbooked == []                                                                # der freie Eingang ist durch den Ausgangs-Satz gedeckt
    result = _handler(world, *PERIOD)
    assert message_of(result) == 'Nicht exportiert: ' + DOUBTFUL_ONE + '2026-03-02 100,00 EUR' + END_ONE + SUFFIX
    assert len(_stack(world)) == 2                                                             # der Spiegel und der Ausgangs-Satz: beide bleiben


def test_a_mirror_without_a_form_is_named_like_a_legacy_mirror(world):
    legacy, _out, _out_row, _free_in = _legacy_next_to_a_booked_transfer(world)
    _execute(world.db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', (legacy,))
    assert [entry[0] for entry in world.db.check_transfers(*PERIOD).doubtful] == [legacy]


def test_a_legacy_mirror_is_named_within_the_neighbourhood_only(world):
    legacy, _out, _out_row, _free_in = _legacy_next_to_a_booked_transfer(world, legacy_day='2026-03-16')                 # 14 Tage danach
    assert [entry[0] for entry in world.db.check_transfers(*PERIOD).doubtful] == [legacy]
    _execute(world.db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', ('2026-03-17', legacy))                     # 15 Tage danach
    assert world.db.check_transfers(*PERIOD).doubtful == []


def test_a_legacy_mirror_alone_is_not_named(world):
    _book(world, None, '2026-03-02', '100.00', 1800, 1810, form='mirror_legacy')
    assert world.db.check_transfers(*PERIOD).doubtful == []
    assert len(_rows_of(_handler(world, *PERIOD))) == 1


def test_a_legacy_mirror_next_to_a_booked_transfer_of_another_amount_or_other_accounts_is_not_named(world):
    _book(world, None, '2026-03-02', '99.00', 1800, 1810, form='mirror_legacy')
    _book(world, None, '2026-03-02', '100.00', 1800, 1600, form='mirror_legacy')                      # Kasse statt B
    _out_booked(world, '2026-03-02', '100.00')
    assert world.db.check_transfers(*PERIOD).doubtful == []


# ═════════════════════════════════════════════════════════════════════════════
# G. Datums- und Betragsformen (p13): nur JJJJ-MM-TT und ganze Zahlen nehmen teil
# ═════════════════════════════════════════════════════════════════════════════

def _transfer_with_one_date(world, date_text):
    """Ausgang am 5.3. (Satz mit ``date_text`` als Datum, so, wie ihn Roh-SQL oder ein fremder Import hinterlassen könnte), Eingang am
    6.3. kontiert."""
    _out, out_row = _out_booked(world, '2026-03-05', '300.00')
    _in_bank, in_row = _in_booked(world, '2026-03-06', '300.00')
    _execute(world.db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', (date_text, out_row))
    return in_row


@pytest.mark.parametrize('date_text', [
    pytest.param('20260305', id='ohne-Bindestriche'),
    pytest.param('2026-03-31 10:00', id='mit-Uhrzeit'),
    pytest.param('2026-W10-4', id='Wochendatum'),
    pytest.param('2026-02-30', id='kein-Kalendertag'),
    pytest.param('', id='leer'),
    pytest.param('gestern', id='Text'),
])
def test_an_entry_with_a_date_that_is_no_iso_day_takes_no_part_and_nothing_is_lost(world, date_text):
    """Der Ausgangs-Satz mit einem Datum in anderer Schreibweise ist kein Bein: Seine Bankbewegung gilt als anders kontiert, der
    Eingangs-Satz fällt nicht weg, sondern wird (als möglicherweise doppelt) genannt. Läse die Rechnung das Datum nachsichtig, legte
    sie den Eingangs-Satz zusammen, obwohl der Ausgangs-Satz mit diesem Datum nicht im Stapel steht: Der ganze Übertrag fehlte."""
    in_row = _transfer_with_one_date(world, date_text)
    assert world.db.duplicate_transfer_rows() == set()
    check = world.db.check_transfers(*PERIOD)
    assert [entry[0] for entry in check.doubtful] == [in_row] and check.trigger == ('2026-03-05', Decimal('300'))
    assert check.unbooked == []
    result = _handler(world, *PERIOD)
    assert is_confirm_redirect(result) and message_of(result).startswith('Nicht exportiert: ' + DOUBTFUL_ONE + '2026-03-06 300,00 EUR')
    rows = _stack(world)
    assert any(fields[_SPALTE_KONTO] == '1800' and fields[_SPALTE_GEGENKONTO] == '1810' for fields in rows)       # der Eingangs-Satz steht im Stapel


@pytest.mark.parametrize('border', ['0001-01-03', '9999-12-30', '1899-12-31', '2201-01-01'])
@pytest.mark.parametrize('target', ['bank movement', 'entry'])
def test_a_border_date_does_not_make_the_export_fail(world, target, border):
    """``0001-01-03`` minus 14 Tage und ``9999-12-30`` plus 14 Tage laufen über das Datumsende – die Zeile nimmt gar nicht teil, der
    Export läuft, keine Meldung mit Python-Text."""
    out, out_row = _out_booked(world, '2026-03-05', '300.00')
    _in_booked(world, '2026-03-06', '300.00')
    _expense(world, '2026-03-10')
    _execute(world.db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', (border, out if target == 'bank movement' else out_row))
    result = _handler(world, *PERIOD)
    if isinstance(result[0], bytes):
        rows = _rows_of(result)
    else:
        assert is_confirm_redirect(result), result
        assert 'Error' not in message_of(result) and 'datetime' not in message_of(result) and 'Traceback' not in message_of(result)
        rows = _stack(world)
    assert rows and world.db.duplicate_transfer_rows() == set()


def test_a_text_amount_outside_the_period_does_not_make_the_export_fail(world):
    """Eine Bankbewegung mit einem Text als Betrag (Roh-SQL) irgendwo im Bestand: Sie nimmt nicht teil, der Export des Zeitraums läuft."""
    old = _bank(world, world.b, '2019-05-10', '-10.00')
    _execute(world.db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', ('abc', old))
    _expense(world, '2026-03-10')
    rows = _rows_of(_handler(world, *PERIOD))
    assert len(rows) == 1 and world.db.duplicate_transfer_rows() == set()


def test_a_text_amount_with_a_child_outside_the_period_does_not_take_part_either(world):
    """Auch mit Satz darunter: Weder die Bankbewegung (Text als Betrag) noch ihr Spiegel-Satz bilden ein Bein."""
    old = _bank(world, world.a, '2019-05-10', '-10.00')
    _book(world, old, '2019-05-10', '-10.00', 1810, 1800)
    _execute(world.db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', ('abc', old))
    _out_booked(world, '2026-03-05', '300.00')
    _in_booked(world, '2026-03-06', '300.00')
    assert len(world.db.duplicate_transfer_rows()) == 1
    assert len(_rows_of(_handler(world, *PERIOD))) == 1


def test_a_text_amount_without_a_child_in_the_period_is_not_listed_and_does_not_make_the_export_fail(world):
    bad = _bank(world, world.a, '2026-03-10', '-10.00')
    _execute(world.db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', ('abc', bad))
    _expense(world, '2026-03-11')
    assert world.db.find_unbooked_bank_movements(*PERIOD) == []
    assert len(_rows_of(_handler(world, *PERIOD))) == 1


# ═════════════════════════════════════════════════════════════════════════════
# H. Die Länge der Weiterleitung (p14, Fall 4)
# ═════════════════════════════════════════════════════════════════════════════

def test_the_redirect_with_three_hints_of_the_largest_amounts_stays_short(world):
    """Drei Hinweise mit je sieben Einträgen (genannt werden fünf) und Beträgen bis knapp unter einer Milliarde: Die Adresse bleibt unter
    2 000 Zeichen – nie wächst sie mit dem Bestand."""
    for number in range(1, 8):
        _bank(world, world.a, f'2026-04-{number:02d}', '-999999999.99')                       # Hinweis 1: sieben freie Bewegungen
        _out_booked(world, f'2026-03-{24 + number:02d}', '888888888.88')                      # Hinweis 2: Ausgang im März, Eingang im April
        _in_booked(world, f'2026-04-{number:02d}', '888888888.88')
    for number in range(10, 17):                                                                # Hinweis 3: eigenständiger Spiegel neben kontiertem Ausgang
        _out_booked(world, f'2026-04-{number:02d}', '777777777.77')
        _book(world, None, f'2026-04-{number:02d}', '777777777.77', 1800, 1810)
    result = _handler(world, '2026-04-01', '2026-04-30')
    assert is_confirm_redirect(result)
    assert len(result[1]) < 2000 and message_of(result).count('weitere') == 3


# ═════════════════════════════════════════════════════════════════════════════
# I. Die bekannten Grenzen – jede als Test benannt, damit sie nicht vergessen wird
# ═════════════════════════════════════════════════════════════════════════════

def test_known_limit_a_batch_transfer_against_two_credits_of_another_denomination_stays_doubled_without_a_hint(world):
    """Sammelauftrag: A −200 mit EINEM Spiegel über 200, auf B zwei Gutschriften über je 100 mit je einem Spiegel. Die Beträge sind
    verschieden, also sind es verschiedene Gruppen – nichts fällt weg, und es gibt keinen Hinweis (zwei Gruppen, die einander nicht
    sehen). Der Stapel trägt 1810 doppelt (400,00 statt 200,00): die Grenze der Zählregel, bekannt und in DB_MODEL.md beschrieben."""
    out = _bank(world, world.a, '2026-03-02', '-200.00')
    _book(world, out, '2026-03-02', '-200.00', 1810, 1800)
    for _ in range(2):
        _in_booked(world, '2026-03-02', '100.00')
    check = world.db.check_transfers(*PERIOD)
    assert check.omitted == set() and check.doubtful == [] and check.unbooked == [] and check.elsewhere == []
    rows = _rows_of(_handler(world, *PERIOD))                                                  # kein Hinweis: direkt der Stapel
    assert len(rows) == 3 and _balance(rows, 1810) == 40_000                                    # im Stapel +400,00 auf 1810, auf der Bank nur +200,00


def test_known_limit_a_batch_transfer_with_a_supplier_payment_beside_it_stays_doubled_without_a_hint(world):
    """Wie oben, dazu eine Lieferantenzahlung über 50,00 ohne Satz am 6.3.: Sie gleicht dem Betrag der Gutschriften und gilt als durch sie
    gedeckt (zwei Gutschriften über je 50 – sie sind verbraucht), kein Hinweis. Die Überzählung bleibt."""
    out = _bank(world, world.a, '2026-03-02', '-100.00')
    _book(world, out, '2026-03-02', '-100.00', 1810, 1800)
    for _ in range(2):
        _in_booked(world, '2026-03-03', '50.00')
    _bank(world, world.a, '2026-03-06', '-50.00')
    check = world.db.check_transfers(*PERIOD)
    assert check.unbooked == [] and check.doubtful == [] and check.omitted == set()
    rows = _rows_of(_handler(world, *PERIOD))
    assert len(rows) == 3 and _balance(rows, 1800) == -20_000 and _balance(rows, 1810) == 20_000              # Bank: 1800 −150,00 (mit der Zahlung), 1810 +100,00


def test_known_limit_two_accounts_with_the_same_number_keep_both_entries_of_a_transfer_without_a_hint(world):
    """Zwei eigene Konten tragen dieselbe SKR-Nummer 1800: Ein Satz auf 1800 lässt sich keinem der beiden zuordnen, keins von beiden
    nimmt teil. Der beidseitig kontierte Übertrag A → B steht mit beiden Sätzen im Stapel (1810 doppelt), und es gibt keinen Hinweis –
    die Rechnung nennt solche Sätze nicht (die Grenze ist in DB_MODEL.md beschrieben)."""
    world.db.insert_account('Hints-Drei', 'Owner', 'DE00HINTS3', 'BIC', 'Bank', is_cash=0, skr_account=1800)
    _out_booked(world, '2026-03-02', '100.00')
    _in_booked(world, '2026-03-02', '100.00')
    check = world.db.check_transfers(*PERIOD)
    assert check.omitted == set() and check.doubtful == [] and check.elsewhere == [] and check.unbooked == []
    rows = _rows_of(_handler(world, *PERIOD))
    assert len(rows) == 2 and _balance(rows, 1810) == 20_000


def test_known_limit_a_standalone_mirror_next_to_a_payment_booked_otherwise_is_not_named(world):
    """Ein Import-Spiegel („1810 an 1800“, ohne Bankbewegung) und ein Ausgang gleicher Höhe, den der Nutzer als Zahlung kontiert hat
    (``purpose_first``, kein Spiegel): Beide Zeilen stehen im Stapel, der dritte Hinweis nennt nur den Spiegel neben einem Bein mit Spiegel
    – hier schweigt er (Grenze der Regel, in DB_MODEL.md beschrieben)."""
    _book(world, None, '2026-03-02', '100.00', 1810, 1800, form='mirror_coa')
    out = _bank(world, world.a, '2026-03-02', '-100.00')
    _book(world, out, '2026-03-02', '-100.00', 1810, 1800, form='purpose_first')
    check = world.db.check_transfers(*PERIOD)
    assert check.doubtful == [] and check.omitted == set() and check.unbooked == []
    assert len(_rows_of(_handler(world, *PERIOD))) == 2


def test_known_limit_a_text_amount_on_a_movement_with_entries_in_the_period_stops_the_split_check(world):
    """Nicht Teil der Zählregel: Die Prüfung der Splits (``find_unbalanced_splits``, vor den Hinweisen) rechnet die Beträge der
    Bankbewegung und ihrer Sätze zusammen – steht ein Text darin (Roh-SQL), bricht sie ab, und der Export antwortet mit einer
    Fehlermeldung statt einer Datei. Nichts wird exportiert, nichts gestempelt."""
    bank = _bank(world, world.a, '2026-03-10', '-10.00')
    _book(world, bank, '2026-03-10', '-10.00', 6815, 1800)
    _execute(world.db, 'UPDATE Bookings SET Amount = ? WHERE ID = ?', ('abc', bank))
    result = _handler(world, *PERIOD)
    assert result[0] == 303 and result[1].startswith('/miscellaneous?datev_export=error&msg=')
    con = sqlite3.connect(world.db.db_name)
    try:
        assert con.execute('SELECT COUNT(*) FROM Bookings WHERE DateTax IS NOT NULL AND DateTax <> ""').fetchone()[0] == 0
    finally:
        con.close()


# ═════════════════════════════════════════════════════════════════════════════
# J. Leistung
# ═════════════════════════════════════════════════════════════════════════════

def test_twenty_thousand_payments_booked_otherwise_next_to_the_transfers_are_counted_in_seconds(world):
    """5 000 Überträge über denselben Betrag (ein einziger Abschnitt) und 5 000 anders kontierte Ausgänge dieses Betrags: Das Umfeld
    wird je Abschnitt mit Halbierungssuche gezählt, nicht je Bewegung durchgesehen."""
    count = 5_000
    db = world.db
    coa_a, coa_b, coa_expense = _coa(db, 1800), _coa(db, 1810), _coa(db, 6815)
    con = sqlite3.connect(db.db_name)
    try:
        next_id = con.execute('SELECT COALESCE(MAX(ID), 0) FROM Bookings').fetchone()[0] + 1
        rows = []
        for i in range(count):
            when = (date(2026, 1, 1) + timedelta(days=i % 365)).isoformat()
            first = next_id + 6 * i
            rows += [(first, when, world.a, None, None, -1_000_000, 'bank', None, None),
                     (first + 1, when, None, coa_b, coa_a, -1_000_000, 'entry', first, 'mirror_counter'),
                     (first + 2, when, world.b, None, None, 1_000_000, 'bank', None, None),
                     (first + 3, when, None, coa_a, coa_b, 1_000_000, 'entry', first + 2, 'mirror_counter'),
                     (first + 4, when, world.a, None, None, -1_000_000, 'bank', None, None),
                     (first + 5, when, None, coa_expense, coa_a, -1_000_000, 'entry', first + 4, 'purpose_first')]
        con.executemany('INSERT INTO Bookings (ID, DateBooking, Account_ID, COA_ID, CounterCOA_ID, Amount, BookingType, '
                        'ParentBooking_ID, EntryForm, Currency) VALUES (?,?,?,?,?,?,?,?,?,"EUR")', rows)
        con.commit()
    finally:
        con.close()
    started = time.perf_counter()
    check = world.db.check_transfers('2026-01-01', '2026-12-31')
    elapsed = time.perf_counter() - started
    assert len(check.omitted) == count and check.unbooked == [] and check.elsewhere == []
    assert check.doubtful == [] and check.trigger is None                                       # zusammengelegt; kein anders kontierter Eingang
    assert elapsed < 15, f'{elapsed:.1f} s für {6 * count} Zeilen'
