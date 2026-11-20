# SPDX-License-Identifier: AGPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 unsix IT Engineering
# Kommerzielle Lizenz ohne AGPL-Pflichten verfügbar – Kontakt: office@unsix.com
"""DATEV-Export: Hinweise zum Stapel und die ausdrückliche Bestätigung (2.3.4).

Der Stapel enthält nur Buchungssätze. Eine Bankbewegung ohne Satz steht deshalb nicht darin, ein Übertrag, dessen Eingangs-Satz
der Stapel weglässt, weil die Ausgangsseite in einem anderen Zeitraum steht, fehlt in diesem Stapel ebenfalls, und ein Eingangs-Satz,
den der Stapel behält, obwohl im Umfeld ein Ausgang mit Satz liegt, steht möglicherweise doppelt darin – im Parallelbetrieb mit einer
anderen Buchhaltung kann das gewollt sein, darf aber nicht still geschehen. Der Export verlangt dann eine Bestätigung, und die gilt nur
für genau diesen Hinweis (Prüfsumme über Zeitraum und Wortlaut). Geprüft werden

- ``Database.find_unbooked_bank_movements``: was als „ohne Buchungssatz“ zählt (kein einziges Kind; Betrag 0, ein Kind mit Datum
  außerhalb des Zeitraums, eigene Konten) und wann die Gegenseite eines Übertrags die Bewegung deckt – nach der Zählregel
  (``db.matching.settle_transfers``): nur wenn die überschüssigen Spiegel der Gegenseite – gezählt gegen die Sätze der anderen Seite
  im Umfeld – für alle Bewegungen ohne Satz ihrer Seite reichen; nie ein Spiegel unter demselben Konto, nie ``mirror_legacy``,
  nie über 1460, und ein ganz unkontierter Übertrag wird auch zwischen kontierten gemeldet
- ``handle_datev_export``: Wortlaut der drei Hinweise, die Prüfsumme, nichts exportiert und nichts gestempelt ohne gültige
  Bestätigung, Bestand oder Zeitraum ändern sich zwischen Hinweis und Bestätigung, die Reihenfolge der Prüfungen, keine Texte oder
  Empfänger in der Adresse
- die Seite *Sonstiges*: Pflicht-Kästchen nur bei gültigen Daten und gültiger Prüfsumme, alles maskiert, GET schreibt nichts
- den Durchlauf über den echten Server: POST ohne Bestätigung, GET der Weiterleitung, POST mit Bestätigung
- ``handle_db_export``: eine feste Meldung statt des Fehlertextes in der Adresse

Die Zählregel selbst steht in test_datev_transfer_duplicates.py. Alle Werte sind erfundene Testdaten.
"""
import hashlib
import os
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from html import escape
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from db.matching import TRANSFER_DAY_WINDOW, TRANSFER_NEIGHBOUR_DAYS, TransferCheck
from server import handlers
from server.pages_miscellaneous import (
    DATEV_CONFIRM_LABEL, DATEV_CONFIRM_NOTICE, DATEV_TRANSFER_NOTE, PageMiscellaneous)
from tests.datev_helpers import (
    check_of, confirmed_export, datev_post, is_confirm_redirect, message_of, query_of)
from tests.http_client import get, own_origin, post

PERIOD = ('2026-03-01', '2026-03-31')
YEAR = ('2026-01-01', '2026-12-31')
CHECK = '0123456789abcdef'                       # eine Prüfsumme in der erwarteten Form (16 Kleinbuchstaben und Ziffern)
SUFFIX = ' Bitte bestätigen, um trotzdem zu exportieren.'


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _coa(db, number):
    return db.get_coa_id_by_account_number(number)


def _make_world(db):
    """Zwei eigene Bankkonten (SKR 1800 und 1810) neben der Standard-Kasse (SKR 1600)."""
    def account(name, skr):
        db.insert_account(name, 'Owner', 'DE00UNBOOKED', 'BIC', 'Bank', is_cash=0, skr_account=skr)
        return next(a[0] for a in db.fetch_accounts() if a[1] == name)
    cash = next(a[0] for a in db.fetch_accounts() if a[6] == 1)
    return SimpleNamespace(db=db, a=account('Unbooked-Eins', 1800), b=account('Unbooked-Zwei', 1810), cash=cash)


@pytest.fixture
def world(tmp_db):
    return _make_world(tmp_db)


def _bank(world, account, day_text, amount, **fields):
    return world.db.insert_booking(day_text, Decimal(amount), account_id=account, booking_type='bank', **fields)


def _entry(db, parent, day_text, amount, coa, counter, form=None, **fields):
    """Buchungssatz unter einer Bankbewegung (``parent``) oder – ohne – eigenständig. Ohne ``form`` leitet ihn die Datenbank
    aus den Konten ab, wie die Buchungsmaske (ein Spiegel wird ``mirror_counter``)."""
    return db.insert_booking(day_text, Decimal(amount), coa_id=_coa(db, coa), counter_coa_id=_coa(db, counter),
                             booking_type='entry', parent_booking_id=parent, entry_form=form, **fields)


def _out_booked(world, day_text='2026-03-10', amount='100.00'):
    """Ausgang auf A mit dem Spiegel „1810 / 1800“, wie ihn die Buchungsmaske schreibt."""
    bank = _bank(world, world.a, day_text, f'-{amount}')
    _entry(world.db, bank, day_text, f'-{amount}', 1810, 1800)
    return bank


def _in_booked(world, day_text='2026-03-10', amount='100.00'):
    """Eingang auf B mit dem Spiegel „1800 / 1810“, wie ihn die Buchungsmaske schreibt."""
    bank = _bank(world, world.b, day_text, amount)
    _entry(world.db, bank, day_text, amount, 1800, 1810)
    return bank


def _shifted(day_text, offset):
    return (date.fromisoformat(day_text) + timedelta(days=offset)).isoformat()


def _expense(world, day_text, amount='-10.00'):
    """Ein eigenständiger Aufwandssatz – damit ein Stapel nicht leer ist."""
    return world.db.insert_booking(day_text, Decimal(amount), coa_id=_coa(world.db, 6815),
                                   counter_coa_id=_coa(world.db, 1800), booking_type='entry')


def _execute(db, sql, params=()):
    con = sqlite3.connect(db.db_name)
    try:
        con.execute(sql, params)
        con.commit()
    finally:
        con.close()


def _dump(db):
    con = sqlite3.connect(db.db_name)
    try:
        return list(con.iterdump())
    finally:
        con.close()


def _date_taxes(db):
    con = sqlite3.connect(db.db_name)
    try:
        return con.execute('SELECT ID, DateTax FROM Bookings ORDER BY ID').fetchall()
    finally:
        con.close()


def _listed(world, period=PERIOD):
    return world.db.find_unbooked_bank_movements(*period)


def _ids(rows):
    return [row[0] for row in rows]


def _post(date_from='2026-03-01', date_to='2026-03-31', **more):
    return datev_post(date_from, date_to, **more)


def _data_lines(csv_bytes):
    return [line for line in csv_bytes.decode('cp1252').split('\r\n') if line][2:]


# ═════════════════════════════════════════════════════════════════════════════
# A. Was „ohne Buchungssatz“ heißt
# ═════════════════════════════════════════════════════════════════════════════

def test_a_bank_movement_without_a_child_is_listed_with_date_and_amount_sorted_by_date(world):
    later = _bank(world, world.a, '2026-03-10', '-100.00')
    earlier = _bank(world, world.b, '2026-03-02', '25.50')
    assert _listed(world) == [(earlier, '2026-03-02', Decimal('25.50')), (later, '2026-03-10', Decimal('-100.00'))]


def test_movements_of_one_day_are_sorted_by_id(world):
    first = _bank(world, world.b, '2026-03-05', '1.00')
    second = _bank(world, world.a, '2026-03-05', '-2.00')
    assert _ids(_listed(world)) == [first, second]


def test_a_movement_with_a_child_is_booked(world):
    movement = _bank(world, world.a, '2026-03-10', '-50.00')
    _entry(world.db, movement, '2026-03-10', '-50.00', 6815, 1800)
    assert _listed(world) == []


def test_a_child_dated_outside_the_period_still_counts_as_booked(world):
    """Zugeordnete Bestandsbuchungen behalten ihr eigenes Datum – dieselbe Sicht wie „offen“ in ``find_unbalanced_splits``."""
    movement = _bank(world, world.a, '2026-03-12', '-60.00')
    _entry(world.db, movement, '2026-05-02', '-60.00', 6815, 1800)
    assert _listed(world) == []


def test_a_movement_of_zero_is_not_listed(world):
    _bank(world, world.a, '2026-03-10', '0.00')
    assert _listed(world) == []


def test_a_movement_with_an_account_of_its_own_but_without_a_child_is_listed(world):
    """Weder EÜR noch Stapel kennen sie: das Konto an der Bankzeile selbst ist kein Buchungssatz."""
    movement = _bank(world, world.a, '2026-03-10', '-40.00', coa_id=_coa(world.db, 6815))
    assert _ids(_listed(world)) == [movement]


def test_a_cash_movement_without_a_child_is_listed(world):
    movement = _bank(world, world.cash, '2026-03-10', '-12.00')
    assert _ids(_listed(world)) == [movement]


def test_only_movements_inside_the_period_count_and_its_ends_are_inside(world):
    _bank(world, world.a, '2026-02-28', '-1.00')
    first = _bank(world, world.a, '2026-03-01', '-2.00')
    last = _bank(world, world.a, '2026-03-31', '-3.00')
    _bank(world, world.a, '2026-04-01', '-4.00')
    assert _ids(_listed(world)) == [first, last]


def test_a_standalone_entry_is_no_bank_movement(world):
    _entry(world.db, None, '2026-03-10', '-50.00', 6815, 1800)
    assert _listed(world) == []


def test_the_search_only_reads(world):
    _out_booked(world)                                                         # deckt die Gegenseite
    _bank(world, world.b, '2026-03-10', '100.00')
    _bank(world, world.a, '2026-03-20', '-5.00')                               # nicht gedeckt
    before = _dump(world.db)
    assert len(_listed(world)) == 1
    assert _dump(world.db) == before


def test_check_transfers_gives_the_four_lists_from_one_state(world):
    """Eine Abfrage für den Handler: satzlose Bewegungen, weggelassene Sätze (ganzer Bestand), die Überträge über den Zeitraum und die
    Sätze, die möglicherweise doppelt im Stapel stehen."""
    free = _bank(world, world.a, '2026-03-20', '-5.00')
    _out_booked(world, '2026-02-27', '700.00')
    _in_booked(world, '2026-03-01', '700.00')
    _out_booked(world, '2026-03-03', '250.00')
    late = _in_booked(world, '2026-03-12', '250.00')                                       # neun Tage danach: eigener Abschnitt, bleibt
    check = world.db.check_transfers(*PERIOD)
    assert _ids(check.unbooked) == [free] and len(check.omitted) == 1
    assert [(day_text, amount) for _id, day_text, amount in check.elsewhere] == [('2026-03-01', Decimal('700.00'))]
    assert [(day_text, amount) for _id, day_text, amount in check.doubtful] == [('2026-03-12', Decimal('250.00'))]
    assert [row[0] for row in world.db.get_child_bookings_for_bank(late)] == [check.doubtful[0][0]]      # der Satz unter dem späten Eingang
    assert world.db.check_transfers('2026-02-01', '2026-02-28').elsewhere == []        # im Februar fiel nichts weg
    assert world.db.check_transfers('2026-02-01', '2026-02-28').doubtful == []
    assert world.db.duplicate_transfer_rows() == check.omitted


# ═════════════════════════════════════════════════════════════════════════════
# B. Wann die Gegenseite eines Übertrags eine Bewegung ohne Satz deckt (Zählregel)
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('offset', [0, 1, 3, TRANSFER_DAY_WINDOW, -TRANSFER_DAY_WINDOW])
@pytest.mark.parametrize('booked_side', ['outgoing', 'incoming'])
def test_the_free_other_side_of_a_booked_transfer_is_covered_inside_the_window(world, booked_side, offset):
    if booked_side == 'outgoing':
        _out_booked(world, '2026-03-10')
        _bank(world, world.b, _shifted('2026-03-10', offset), '100.00')
    else:
        _in_booked(world, '2026-03-10')
        _bank(world, world.a, _shifted('2026-03-10', offset), '-100.00')
    assert _listed(world, YEAR) == []


@pytest.mark.parametrize('offset', [TRANSFER_DAY_WINDOW + 1, -TRANSFER_DAY_WINDOW - 1, 30])
@pytest.mark.parametrize('booked_side', ['outgoing', 'incoming'])
def test_a_free_movement_outside_the_window_is_in_a_section_of_its_own_and_reported(world, booked_side, offset):
    if booked_side == 'outgoing':
        _out_booked(world, '2026-03-10')
        free = _bank(world, world.b, _shifted('2026-03-10', offset), '100.00')
    else:
        _in_booked(world, '2026-03-10')
        free = _bank(world, world.a, _shifted('2026-03-10', offset), '-100.00')
    assert _ids(_listed(world, YEAR)) == [free]


def test_a_customer_payment_next_to_a_transfer_booked_on_one_side_leaves_both_free_movements_reported(world):
    """Übertrag A → B über 500,00 nur auf A kontiert; auf B die Gegenseite UND eine Kundenzahlung gleicher Höhe, beide ohne Satz.
    Ein Spiegel, der beliebig viele gleich hohe Bewegungen deckt, ließe beide ungemeldet. Hier reicht der eine überschüssige Ausgang nicht für zwei
    Eingänge ohne Satz (b_c = 2 > a_m − b_m = 1): keiner von beiden ist gedeckt, beide werden gemeldet."""
    _out_booked(world, '2026-03-02', '500.00')
    counterpart = _bank(world, world.b, '2026-03-02', '500.00')
    customer = _bank(world, world.b, '2026-03-05', '500.00')
    assert _ids(_listed(world)) == [counterpart, customer]


def test_two_outgoing_movements_of_the_same_amount_are_not_covered_by_the_one_booked_incoming_entry(world):
    """Das Spiegelbild: Auf B der Eingang kontiert, auf A zwei Ausgänge ohne Satz – ein Eingang deckt nur einen."""
    _in_booked(world, '2026-03-02', '500.00')
    first = _bank(world, world.a, '2026-03-02', '-500.00')
    second = _bank(world, world.a, '2026-03-04', '-500.00')
    assert _ids(_listed(world)) == [first, second]


@pytest.mark.parametrize('free_side', ['outgoing', 'incoming'])
def test_a_free_payment_of_the_same_amount_next_to_a_transfer_booked_on_both_sides_is_reported(world, free_side):
    """Beidseitig kontierter Übertrag über 500,00 und zwei Tage später eine Zahlung gleicher Höhe auf A (Lieferant, Ausgang) oder auf
    B (Kunde, Eingang), ohne Satz: Ein Spiegel, der sie „deckte“, ließe sie ungemeldet. Hier: a_m = b_m, kein Überschuss – die
    Zahlung wird gemeldet."""
    _out_booked(world, '2026-03-02', '500.00')
    _in_booked(world, '2026-03-02', '500.00')
    if free_side == 'outgoing':
        payment = _bank(world, world.a, '2026-03-04', '-500.00')
    else:
        payment = _bank(world, world.b, '2026-03-04', '500.00')
    assert _ids(_listed(world)) == [payment]


@pytest.mark.parametrize('form', ['mirror_legacy', 'mirror_counter', 'mirror_coa', None])
def test_a_mirror_under_the_outgoing_account_covers_no_incoming_movement_on_that_account(world, form):
    """Ein Spiegel unter dem Ausgang auf A (egal in welcher Form, ``mirror_legacy`` und ohne Form eingeschlossen) deckt keine
    Kundenzahlung auf A: Seine Richtung A → B (nach dem Vorzeichen des Ausgangs) hat mit einem Eingang auf A nichts zu tun. Die
    Gegenseite auf B dagegen ist gedeckt."""
    out_bank = _bank(world, world.a, '2026-03-02', '-500.00')
    mirror = _entry(world.db, out_bank, '2026-03-02', '-500.00', 1810, 1800, form=form)
    if form is None:
        _execute(world.db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', (mirror,))
    customer = _bank(world, world.a, '2026-03-04', '500.00')
    _bank(world, world.b, '2026-03-02', '500.00')                              # die Gegenseite des Übertrags
    assert _ids(_listed(world)) == [customer]


def test_a_second_outgoing_movement_of_the_same_amount_on_the_same_account_is_no_counterpart(world):
    """Zwei Ausgänge gleicher Höhe auf A, einer mit Satz, einer ohne: Der Ausgang mit Satz deckt den ohne nicht."""
    _out_booked(world, '2026-03-02', '500.00')
    other = _bank(world, world.a, '2026-03-03', '-500.00')
    assert _ids(_listed(world)) == [other]


def test_two_equal_transfers_each_booked_on_one_side_only_report_both_free_movements_in_one_section(world):
    """Zwei gleich hohe Überträge, der erste nur auf A, der zweite nur auf B kontiert – am selben Tag oder im Abstand des Fensters:
    ein Abschnitt, in dem sich die Gegenseiten nicht unterscheiden lassen. Beide Bewegungen ohne Satz werden gemeldet (nichts geht
    verloren: beide Sätze bleiben im Stapel, siehe test_datev_transfer_duplicates.py)."""
    _out_booked(world, '2026-03-02', '500.00')
    b1 = _bank(world, world.b, '2026-03-02', '500.00')
    a2 = _bank(world, world.a, _shifted('2026-03-02', TRANSFER_DAY_WINDOW), '-500.00')
    _in_booked(world, _shifted('2026-03-02', TRANSFER_DAY_WINDOW), '500.00')
    assert _ids(_listed(world)) == [b1, a2]


def test_two_equal_transfers_booked_on_one_side_only_beyond_the_window_report_both_free_movements_through_the_neighbourhood(world):
    """Acht Tage auseinander: zwei Abschnitte, aber im Umfeld jedes Abschnitts steht der Satz des anderen Übertrags – die Deckung
    rechnet mit ihm (wessen Gegenseite die satzlose Bewegung ist, lässt sich nicht sagen). Beide Bewegungen werden gemeldet."""
    _out_booked(world, '2026-03-02', '500.00')
    b1 = _bank(world, world.b, '2026-03-02', '500.00')
    later = _shifted('2026-03-02', TRANSFER_DAY_WINDOW + 1)
    a2 = _bank(world, world.a, later, '-500.00')
    _in_booked(world, later, '500.00')
    assert _ids(_listed(world)) == [b1, a2]


def test_two_equal_transfers_booked_on_one_side_only_beyond_the_neighbourhood_cover_their_free_movements_each(world):
    """Fünfzehn Tage auseinander: Der Satz des anderen Übertrags liegt nicht mehr im Umfeld – jede Bewegung ohne Satz ist durch
    den Satz ihres eigenen Übertrags gedeckt."""
    _out_booked(world, '2026-03-02', '500.00')
    _bank(world, world.b, '2026-03-02', '500.00')
    later = _shifted('2026-03-02', TRANSFER_NEIGHBOUR_DAYS + 1)
    _bank(world, world.a, later, '-500.00')
    _in_booked(world, later, '500.00')
    assert _listed(world) == []


def test_an_unbooked_transfer_between_booked_ones_is_reported_although_their_entries_lie_nearby(world):
    """Der erste Übertrag am 1.3./9.3. kontiert, der dritte am 21.3./29.3., dazwischen ein ganz unkontierter (9.3./17.3.): Die Sätze
    der Nachbarn standen früher als „Gegenseite“ für ihn – jetzt zählt die Deckung die Ausgänge mit Satz im Umfeld, und beide
    Bewegungen des unkontierten Übertrags werden gemeldet (eine genannte Bewegung hat tatsächlich keinen Buchungssatz)."""
    _out_booked(world, '2026-03-01', '300.00')
    _in_booked(world, '2026-03-09', '300.00')
    free_out = _bank(world, world.a, '2026-03-09', '-300.00')
    free_in = _bank(world, world.b, '2026-03-17', '300.00')
    _out_booked(world, '2026-03-21', '300.00')
    _in_booked(world, '2026-03-29', '300.00')
    assert _ids(_listed(world)) == [free_out, free_in]


def test_a_standalone_mirror_next_to_a_booked_bank_movement_covers_nothing(world):
    """Ein Import-Spiegel („1810 an 1800“) und ein von Hand kontierter Eingang desselben Übertrags: Der Spiegel steht als möglicherweise
    doppelt im Hinweis 3 und zählt nicht als Überschuss – ein zweiter Ausgang gleicher Höhe ohne Satz ist nur durch den Eingangs-Satz
    gedeckt, zwei davon nicht mehr."""
    _entry(world.db, None, '2026-03-05', '120.00', 1810, 1800, form='mirror_coa')
    _in_booked(world, '2026-03-06', '120.00')
    first = _bank(world, world.a, '2026-03-05', '-120.00')
    assert _listed(world) == []
    second = _bank(world, world.a, '2026-03-07', '-120.00')
    assert _ids(_listed(world)) == [first, second]


def test_an_outgoing_movement_without_entry_at_the_end_of_the_period_is_covered_by_the_incoming_entry_of_the_next(world):
    """Ausgang 31.3. noch ohne Satz, Eingang 1.4. kontiert: Der März-Stapel meldet die Bewegung nicht (gedeckt), der Satz des
    Übertrags steht im April-Stapel. Der Abschnitt reicht über die Zeitraumgrenze, weil gezählt wird, nicht nach Zeitraum."""
    _bank(world, world.a, '2026-03-31', '-700.00')
    _in_booked(world, '2026-04-01', '700.00')
    assert _listed(world, ('2026-03-01', '2026-03-31')) == []


def test_a_booked_mirror_just_outside_the_period_covers_a_movement_at_its_edge_and_one_day_more_does_not(world):
    edge = _bank(world, world.a, '2026-03-31', '-100.00')
    in_bank = _in_booked(world, _shifted('2026-03-31', TRANSFER_DAY_WINDOW), '100.00')
    assert _listed(world) == []
    _execute(world.db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ? OR ParentBooking_ID = ?',
             (_shifted('2026-03-31', TRANSFER_DAY_WINDOW + 1), in_bank, in_bank))
    assert _ids(_listed(world)) == [edge]


def test_a_partial_mirror_in_a_split_covers_a_free_movement_of_its_own_amount_only(world):
    """Die Bankbewegung über 500,00 trägt einen Spiegel über 100,00 an B: Eine Bewegung ohne Satz über 100,00 auf B ist gedeckt,
    eine über 500,00 nicht."""
    out_bank = _bank(world, world.a, '2026-03-10', '-500.00')
    _entry(world.db, out_bank, '2026-03-10', '-400.00', 6815, 1800)
    _entry(world.db, out_bank, '2026-03-10', '-100.00', 1810, 1800)
    _bank(world, world.b, '2026-03-10', '100.00')
    full = _bank(world, world.b, '2026-03-10', '500.00')
    assert _ids(_listed(world)) == [full]


@pytest.mark.parametrize('sign', [1, -1], ids=['Betrag-wie-die-Bankbewegung', 'Betrag-umgekehrt'])
@pytest.mark.parametrize('form', ['mirror_legacy', 'mirror_counter', 'mirror_coa'])
def test_the_direction_of_a_leg_is_the_sign_of_the_bank_movement_not_the_form_or_the_sign_of_its_entry(world, form, sign):
    """Der Spiegel unter dem Ausgang auf A trägt, welches Vorzeichen und welche Form er will: Er deckt die Gegenseite auf B."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _entry(world.db, out_bank, '2026-03-10', f'{-100 * sign}.00', 1810, 1800, form=form)
    _bank(world, world.b, '2026-03-10', '100.00')
    assert _listed(world) == []


# ── eigenständige Spiegel ────────────────────────────────────────────────────

def test_a_standalone_wiso_mirror_covers_the_free_movement_on_each_side(world):
    """„1810 an 1800“ mit positivem Betrag (``mirror_coa``, Konto im Soll), ohne Bankbewegung – eine Zeile je Übertrag."""
    _bank(world, world.a, '2026-03-10', '-100.00')
    _bank(world, world.b, '2026-03-11', '100.00')
    _entry(world.db, None, '2026-03-10', '100.00', 1810, 1800, form='mirror_coa')
    assert _listed(world) == []


def test_one_standalone_mirror_covers_one_free_movement_per_side_not_two(world):
    """s = 1: Zwei Ausgänge ohne Satz über denselben Betrag sind durch einen eigenständigen Spiegel nicht gedeckt – keiner von beiden."""
    first = _bank(world, world.a, '2026-03-10', '-100.00')
    second = _bank(world, world.a, '2026-03-11', '-100.00')
    _entry(world.db, None, '2026-03-10', '100.00', 1810, 1800, form='mirror_coa')
    assert _ids(_listed(world)) == [first, second]


def test_a_standalone_mirror_in_the_other_direction_covers_nothing(world):
    """„1800 an 1810“ (A bekommt, B verliert) gehört zu der Gruppe B → A: Weder der Ausgang auf A noch der Eingang auf B passt."""
    out = _bank(world, world.a, '2026-03-10', '-100.00')
    incoming = _bank(world, world.b, '2026-03-10', '100.00')
    _entry(world.db, None, '2026-03-10', '100.00', 1800, 1810, form='mirror_coa')
    assert _ids(_listed(world)) == [out, incoming]


@pytest.mark.parametrize('form', ['mirror_legacy', None])
def test_a_standalone_mirror_of_unknown_reading_covers_nothing(world, form):
    """Welches Konto verliert, sagt ``mirror_legacy`` (oder eine Zeile ohne Form) nicht – nichts wird gedeutet, nichts gedeckt."""
    out = _bank(world, world.a, '2026-03-10', '-100.00')
    incoming = _bank(world, world.b, '2026-03-10', '100.00')
    mirror = _entry(world.db, None, '2026-03-10', '100.00', 1810, 1800, form='mirror_legacy')
    if form is None:
        _execute(world.db, 'UPDATE Bookings SET EntryForm = NULL WHERE ID = ?', (mirror,))
    assert _ids(_listed(world)) == [out, incoming]


# ── was nie deckt ────────────────────────────────────────────────────────────

def test_a_mirror_of_another_amount_covers_nothing(world):
    _out_booked(world, '2026-03-10', '100.00')
    other = _bank(world, world.b, '2026-03-10', '100.01')
    assert _ids(_listed(world)) == [other]


def test_a_mirror_between_the_cash_account_and_a_bank_covers_no_movement_of_another_account(world):
    """Der Ausgang auf A trägt einen Spiegel an die Kasse – eine Bewegung ohne Satz über denselben Betrag auf B gehört nicht dazu."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _entry(world.db, out_bank, '2026-03-10', '-100.00', 1600, 1800)
    other = _bank(world, world.b, '2026-03-10', '100.00')
    assert _ids(_listed(world)) == [other]


@pytest.mark.parametrize('side', [(1810, 1460), (1460, 1810)])
def test_a_transfer_through_the_clearing_account_covers_nothing(world, side):
    """1460 ist kein eigenes Konto, ein Satz mit ihm kein Spiegel: Fehlt die zweite Seite („1810 an 1460“), ist der Alarm berechtigt."""
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _entry(world.db, out_bank, '2026-03-10', '-100.00', 1460, 1800)
    incoming = _bank(world, world.b, '2026-03-10', '100.00')
    assert _ids(_listed(world)) == [incoming]
    _entry(world.db, incoming, '2026-03-10', '100.00', *side)
    assert _listed(world) == []                                                    # mit Satz ist sie keine Bewegung ohne Satz mehr


@pytest.mark.parametrize('form', ['purpose_first', 'liquid_first', 'transfer'])
def test_a_row_that_is_no_mirror_is_no_leg_and_covers_nothing(world, form):
    out_bank = _bank(world, world.a, '2026-03-10', '-100.00')
    _entry(world.db, out_bank, '2026-03-10', '-100.00', 1810, 1800, form=form)
    other = _bank(world, world.b, '2026-03-10', '100.00')
    assert _ids(_listed(world)) == [other]


def test_a_movement_on_an_account_without_skr_number_is_never_covered(world):
    world.db.insert_account('Ohne-SKR', 'Owner', 'DE00UNBOOKED', 'BIC', 'Bank', is_cash=0, skr_account=None)
    account = next(a[0] for a in world.db.fetch_accounts() if a[1] == 'Ohne-SKR')
    _out_booked(world, '2026-03-10', '100.00')
    lonely = _bank(world, account, '2026-03-10', '100.00')
    assert _ids(_listed(world)) == [lonely]


def test_a_movement_on_an_account_that_shares_its_number_with_another_is_never_covered(world):
    """Zwei eigene Konten mit der Nummer 1810: Keins von beiden nimmt teil – die Bewegung ohne Satz auf einem von ihnen wird gemeldet."""
    world.db.insert_account('Unbooked-Drei', 'Owner', 'DE00UNBOOKED', 'BIC', 'Bank', is_cash=0, skr_account=1810)
    third = next(a[0] for a in world.db.fetch_accounts() if a[1] == 'Unbooked-Drei')
    _out_booked(world, '2026-03-10', '100.00')
    lonely = _bank(world, third, '2026-03-10', '100.00')
    on_b = _bank(world, world.b, '2026-03-10', '100.00')
    assert _ids(_listed(world)) == [lonely, on_b]


# ═════════════════════════════════════════════════════════════════════════════
# C. Der erste Hinweis im Handler: Bankbewegungen ohne Buchungssatz
# ═════════════════════════════════════════════════════════════════════════════

def _scenario(world):
    """Ein gebuchter Ausgang (ein Satz im Stapel) und sieben Bankbewegungen ohne Buchungssatz im März 2026; die erste
    trägt Text und Empfänger, die nirgends auftauchen dürfen."""
    unbooked = []
    for day_number, amount in enumerate(['100.00', '-20.50', '30.00', '-5.25', '1.00', '-2.00', '0.50'], start=1):
        extra = dict(recipient_client='GEHEIM-EMPFAENGER', text='GEHEIM-TEXT') if day_number == 1 else {}
        unbooked.append(_bank(world, world.a if day_number % 2 else world.b, f'2026-03-{day_number:02d}', amount, **extra))
    booked = _bank(world, world.a, '2026-03-10', '-50.00', text='Büromaterial')
    _entry(world.db, booked, '2026-03-10', '-50.00', 6815, 1800)
    return unbooked, booked


EXPECTED_MESSAGE = (
    'Nicht exportiert: 7 Bankbewegungen im Zeitraum haben keinen Buchungssatz und stehen nicht im Stapel '
    '(Eingänge 131,50 EUR, Ausgänge 27,75 EUR) – 2026-03-01 100,00 EUR; 2026-03-02 -20,50 EUR; '
    '2026-03-03 30,00 EUR; 2026-03-04 -5,25 EUR; 2026-03-05 1,00 EUR; und 2 weitere.' + SUFFIX)


@pytest.fixture
def no_export(monkeypatch):
    """Ersetzt ``export_to_datev`` durch einen Zähler: Wer hier landet, hat exportiert."""
    from export import datev
    called = []
    monkeypatch.setattr(datev, 'export_to_datev', lambda *args, **kwargs: called.append(args))
    return called


def test_without_a_confirmation_nothing_is_exported_and_the_hint_comes_back(world, no_export):
    _scenario(world)
    before = _date_taxes(world.db)

    result = handlers.handle_datev_export(world.db, _post())

    assert result[0] == 303 and len(result) == 2 and is_confirm_redirect(result)
    query = query_of(result[1])
    assert query['datev_export'] == ['confirm'] and query['datev_from'] == ['2026-03-01'] and query['datev_to'] == ['2026-03-31']
    assert query['datev_check'][0] == check_of(result) and len(query['datev_check'][0]) == 16
    assert no_export == [] and _date_taxes(world.db) == before


def test_the_message_counts_splits_incoming_from_outgoing_and_names_five_movements(world, no_export):
    _scenario(world)
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert message == EXPECTED_MESSAGE
    assert '%' not in message and message.count(' EUR;') == 5 and 'und 2 weitere' in message


def test_with_five_or_fewer_movements_the_message_has_no_further_part(world, no_export):
    for day_number, amount in enumerate(['10.00', '-4.00'], start=1):
        _bank(world, world.a, f'2026-03-{day_number:02d}', amount)
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert message.startswith('Nicht exportiert: 2 Bankbewegungen im Zeitraum haben keinen Buchungssatz und stehen nicht im Stapel')
    assert ('(Eingänge 10,00 EUR, Ausgänge 4,00 EUR) – 2026-03-01 10,00 EUR; 2026-03-02 -4,00 EUR.' + SUFFIX) in message
    assert 'weitere' not in message


def test_one_movement_is_named_in_the_singular(world, no_export):
    _bank(world, world.a, '2026-03-05', '-1.00')
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert message == ('Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
                       '(Eingänge 0,00 EUR, Ausgänge 1,00 EUR) – 2026-03-05 -1,00 EUR.' + SUFFIX)


def test_incoming_and_outgoing_stay_apart_instead_of_adding_up(world, no_export):
    _bank(world, world.a, '2026-03-01', '100.00')
    _bank(world, world.a, '2026-03-02', '-100.00')
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert 'Eingänge 100,00 EUR, Ausgänge 100,00 EUR' in message


def test_the_hint_names_only_the_movements_of_the_period(world, no_export):
    _bank(world, world.a, '2026-02-27', '-9.00')
    _bank(world, world.a, '2026-03-05', '-1.00')
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert message.startswith('Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel')
    assert '2026-02-27' not in message


def test_neither_text_nor_recipient_of_a_bank_movement_goes_into_the_address(world, no_export):
    _scenario(world)
    location = handlers.handle_datev_export(world.db, _post())[1]
    for secret in ('GEHEIM', 'Büro', 'EMPFAENGER', 'TEXT'):
        assert secret not in location and secret not in message_of((303, location))
    assert all(bad not in location for bad in ('<', '>', '"', "'", '\r', '\n', ' '))


def test_through_the_handler_both_free_movements_next_to_a_transfer_booked_on_one_side_are_named_and_confirmed_together(world):
    """Der Übertrag nur auf A kontiert, auf B die Gegenseite und eine Kundenzahlung gleicher Höhe ohne Satz: Der Hinweis nennt beide
    (der eine Spiegel deckt nicht zwei); nach der Bestätigung steht der eine Satz im Stapel."""
    _out_booked(world, '2026-03-02', '500.00')
    _bank(world, world.b, '2026-03-02', '500.00')
    _bank(world, world.b, '2026-03-05', '500.00')
    before = _date_taxes(world.db)

    result = handlers.handle_datev_export(world.db, _post())

    assert message_of(result) == (
        'Nicht exportiert: 2 Bankbewegungen im Zeitraum haben keinen Buchungssatz und stehen nicht im Stapel '
        '(Eingänge 1.000,00 EUR, Ausgänge 0,00 EUR) – 2026-03-02 500,00 EUR; 2026-03-05 500,00 EUR.' + SUFFIX)
    assert _date_taxes(world.db) == before
    assert len(_data_lines(confirmed_export(world.db, *PERIOD)[0])) == 1


def test_through_the_handler_a_customer_payment_on_the_account_of_a_legacy_mirror_is_named_in_the_hint(world):
    """``mirror_legacy`` unter dem Ausgang auf A: Die Gegenseite auf B ist gedeckt, die Kundenzahlung auf A (Eingang) nicht."""
    out_bank = _bank(world, world.a, '2026-03-02', '-500.00')
    _entry(world.db, out_bank, '2026-03-02', '-500.00', 1810, 1800, form='mirror_legacy')
    _bank(world, world.b, '2026-03-02', '500.00')
    _bank(world, world.a, '2026-03-04', '500.00')

    message = message_of(handlers.handle_datev_export(world.db, _post()))

    assert message == ('Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
                       '(Eingänge 500,00 EUR, Ausgänge 0,00 EUR) – 2026-03-04 500,00 EUR.' + SUFFIX)


def test_a_period_without_gaps_exports_without_any_hint(world):
    movement = _bank(world, world.a, '2026-03-10', '-50.00')
    _entry(world.db, movement, '2026-03-10', '-50.00', 6815, 1800)
    _bank(world, world.a, '2026-03-11', '0.00')                                  # Betrag 0: fehlt keine Zeile
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post())
    assert len(_data_lines(csv_bytes)) == 1


def test_a_transfer_with_one_side_booked_asks_for_nothing(world):
    _out_booked(world)
    _bank(world, world.b, '2026-03-11', '100.00')
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post())
    assert len(_data_lines(csv_bytes)) == 1


# ═════════════════════════════════════════════════════════════════════════════
# D. Der zweite Hinweis: Überträge, deren Ausgangsseite in einem anderen Zeitraum steht
# ═════════════════════════════════════════════════════════════════════════════

def _transfers_from_february(world):
    """Sieben Überträge über je 100,00, beidseitig kontiert: Ausgänge am 24.–28.2., Eingänge am 1.–7.3. (eine Kette, ein
    Abschnitt) – im März-Stapel fallen alle sieben Eingangs-Sätze weg, ihre Ausgangsseite steht im Februar."""
    for out_day, in_day in zip(('2026-02-24', '2026-02-25', '2026-02-26', '2026-02-27', '2026-02-27', '2026-02-28', '2026-02-28'),
                               ('2026-03-01', '2026-03-02', '2026-03-03', '2026-03-04', '2026-03-05', '2026-03-06', '2026-03-07')):
        _out_booked(world, out_day)
        _in_booked(world, in_day)
    _expense(world, '2026-03-15')


def test_the_second_hint_names_the_transfers_and_the_first_five_of_them(world, no_export):
    _transfers_from_february(world)
    result = handlers.handle_datev_export(world.db, _post())
    assert is_confirm_redirect(result)
    assert message_of(result) == (
        'Nicht exportiert: 7 Überträge zwischen eigenen Konten stehen mit ihrer Ausgangsseite außerhalb des Zeitraums und deshalb '
        'nicht in diesem Stapel – 2026-03-01 100,00 EUR; 2026-03-02 100,00 EUR; 2026-03-03 100,00 EUR; 2026-03-04 100,00 EUR; '
        '2026-03-05 100,00 EUR; und 2 weitere. Sie gehören in den Stapel des Zeitraums ihrer Ausgangsseite.' + SUFFIX)
    assert no_export == []


def test_the_second_hint_names_one_transfer_in_the_singular(world, no_export):
    _out_booked(world, '2026-03-31', '700.00')
    _in_booked(world, '2026-04-01', '700.00')
    _expense(world, '2026-04-15')
    message = message_of(handlers.handle_datev_export(world.db, _post('2026-04-01', '2026-04-30')))
    assert message == ('Nicht exportiert: 1 Übertrag zwischen eigenen Konten steht mit seiner Ausgangsseite außerhalb des '
                       'Zeitraums und deshalb nicht in diesem Stapel – 2026-04-01 700,00 EUR. Er gehört in den Stapel des '
                       'Zeitraums seiner Ausgangsseite.' + SUFFIX)


def test_when_the_outgoing_side_is_inside_the_period_nothing_is_said(world):
    _out_booked(world, '2026-03-10')
    _in_booked(world, '2026-03-12')
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post())
    assert len(_data_lines(csv_bytes)) == 1


def test_only_the_surplus_of_a_section_is_named_not_every_omitted_entry_of_it(world):
    """Zwei Überträge, ein Ausgang im Februar, einer im März, beide Eingänge im März: Im Stapel stehen ein Ausgangs-Satz und zwei
    weggelassene Eingangs-Sätze – einer davon gehört zum Ausgang im Februar. Der Hinweis zählt je Abschnitt (zwei weggelassene gegen
    einen Ausgang im Zeitraum) und nennt genau einen, den am Rand, über den der Übertrag läuft (den früheren)."""
    _out_booked(world, '2026-02-28')
    _out_booked(world, '2026-03-02')
    _in_booked(world, '2026-03-03')
    _in_booked(world, '2026-03-04')
    _expense(world, '2026-03-15')
    result = handlers.handle_datev_export(world.db, _post())
    assert is_confirm_redirect(result)
    assert message_of(result) == (
        'Nicht exportiert: 1 Übertrag zwischen eigenen Konten steht mit seiner Ausgangsseite außerhalb des Zeitraums und deshalb '
        'nicht in diesem Stapel – 2026-03-03 100,00 EUR. Er gehört in den Stapel des Zeitraums seiner Ausgangsseite.' + SUFFIX)


def test_both_hints_come_in_one_message_first_the_free_movements_then_the_transfers(world):
    _bank(world, world.a, '2026-03-20', '-9.00')
    _out_booked(world, '2026-02-28', '100.00')
    _in_booked(world, '2026-03-02', '100.00')
    _expense(world, '2026-03-15')
    before = _date_taxes(world.db)
    result = handlers.handle_datev_export(world.db, _post())
    assert message_of(result) == (
        'Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
        '(Eingänge 0,00 EUR, Ausgänge 9,00 EUR) – 2026-03-20 -9,00 EUR. 1 Übertrag zwischen eigenen Konten steht mit seiner '
        'Ausgangsseite außerhalb des Zeitraums und deshalb nicht in diesem Stapel – 2026-03-02 100,00 EUR. Er gehört in den '
        'Stapel des Zeitraums seiner Ausgangsseite.' + SUFFIX)
    assert _date_taxes(world.db) == before
    assert len(_data_lines(confirmed_export(world.db, *PERIOD)[0])) == 1               # ein Bestätigen für beide Hinweise: nur der Aufwand bleibt


# ═════════════════════════════════════════════════════════════════════════════
# D2. Der dritte Hinweis: Überträge, die möglicherweise doppelt im Stapel stehen
# ═════════════════════════════════════════════════════════════════════════════

THIRD_CAUSE = ('im Umfeld ist nicht jede Bankbewegung kontiert, die beiden Seiten liegen weit auseinander, oder es gibt zusätzlich '
               'einen eigenständigen Umbuchungssatz')
THIRD_ONE = '1 Übertrag zwischen eigenen Konten steht möglicherweise doppelt im Stapel – ' + THIRD_CAUSE + ': '
THIRD_MANY = ' Überträge zwischen eigenen Konten stehen möglicherweise doppelt im Stapel – ' + THIRD_CAUSE + ': '       # „{n}“ davor
THIRD_END_ONE = '. Bitte die Bankbewegungen dieses Tages prüfen.'
THIRD_END_MANY = '. Bitte die Bankbewegungen dieser Tage prüfen.'
THIRD_TRIGGER = ' (Auslöser im Umfeld: {} {} EUR)'                                    # Datum und Betrag der Bewegung, die sperrt


def _late_transfer(world, out_day='2026-03-02', in_day='2026-03-11', amount='100.00'):
    """Beidseitig kontierter Übertrag, dessen Eingang neun Tage (mehr als das Fenster) nach dem Ausgang liegt: Der Eingangs-Satz
    bleibt im Stapel – im Umfeld liegt ein Ausgang mit Satz."""
    _out_booked(world, out_day, amount)
    return _in_booked(world, in_day, amount)


def test_the_third_hint_names_one_possibly_doubled_transfer_in_the_singular(world, no_export):
    _late_transfer(world)
    result = handlers.handle_datev_export(world.db, _post())
    assert is_confirm_redirect(result)
    assert message_of(result) == ('Nicht exportiert: 1 Übertrag zwischen eigenen Konten steht möglicherweise doppelt im Stapel – im '
                                  'Umfeld ist nicht jede Bankbewegung kontiert, die beiden Seiten liegen weit auseinander, oder es '
                                  'gibt zusätzlich einen eigenständigen Umbuchungssatz: 2026-03-11 100,00 EUR. '
                                  'Bitte die Bankbewegungen dieses Tages prüfen.' + SUFFIX)
    assert message_of(result) == 'Nicht exportiert: ' + THIRD_ONE + '2026-03-11 100,00 EUR' + THIRD_END_ONE + SUFFIX
    assert no_export == []


def test_the_third_hint_names_the_transfers_and_the_first_five_of_them(world, no_export):
    """Sieben Überträge im Abstand von 20 Tagen, jeder Eingang neun Tage nach seinem Ausgang: sieben Eingangs-Sätze bleiben im
    Stapel; genannt werden die ersten fünf nach Datum."""
    for number in range(7):
        out_day = date(2026, 1, 5) + timedelta(days=20 * number)
        _late_transfer(world, out_day.isoformat(), (out_day + timedelta(days=9)).isoformat())
    result = handlers.handle_datev_export(world.db, _post(*YEAR))
    assert message_of(result) == (
        'Nicht exportiert: 7 Überträge zwischen eigenen Konten stehen möglicherweise doppelt im Stapel – im Umfeld ist nicht jede '
        'Bankbewegung kontiert, die beiden Seiten liegen weit auseinander, oder es gibt zusätzlich einen eigenständigen '
        'Umbuchungssatz: 2026-01-14 100,00 EUR; 2026-02-03 100,00 EUR; 2026-02-23 100,00 EUR; 2026-03-15 100,00 EUR; '
        '2026-04-04 100,00 EUR; und 2 weitere. Bitte die Bankbewegungen dieser Tage prüfen.' + SUFFIX)
    assert no_export == []


def test_the_third_hint_names_only_the_entries_of_the_period(world, no_export):
    for number in range(3):
        out_day = date(2026, 1, 5) + timedelta(days=40 * number)
        _late_transfer(world, out_day.isoformat(), (out_day + timedelta(days=9)).isoformat())          # Eingänge 14.1., 23.2., 14.4.
    message = message_of(handlers.handle_datev_export(world.db, _post('2026-02-01', '2026-02-28')))
    assert message == 'Nicht exportiert: ' + THIRD_ONE + '2026-02-23 100,00 EUR' + THIRD_END_ONE + SUFFIX


def test_a_transfer_inside_the_window_is_merged_and_needs_no_third_hint(world):
    _out_booked(world, '2026-03-02')
    _in_booked(world, _shifted('2026-03-02', TRANSFER_DAY_WINDOW))
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post())
    assert len(_data_lines(csv_bytes)) == 1


def test_a_transfer_beyond_the_neighbourhood_needs_no_third_hint_the_known_limit(world):
    """Der Eingang 15 Tage nach dem Ausgang: kein Ausgang mit Satz im Umfeld – nichts wird gemeldet, beide Sätze stehen im Stapel."""
    _out_booked(world, '2026-03-02')
    _in_booked(world, _shifted('2026-03-02', TRANSFER_NEIGHBOUR_DAYS + 1))
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post())
    assert len(_data_lines(csv_bytes)) == 2


def test_a_standalone_mirror_next_to_a_booked_bank_movement_is_named_by_its_own_date_and_amount(world, no_export):
    """Ein Import-Spiegel („1810 an 1800“, ohne Bankbewegung) und ein von Hand kontierter Eingang desselben Übertrags: Der Spiegel
    wird genannt, in dessen Umfeld eine kontierte Bankbewegung liegt."""
    _entry(world.db, None, '2026-03-05', '120.00', 1810, 1800, form='mirror_coa')
    _in_booked(world, '2026-03-06', '120.00')
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert message == 'Nicht exportiert: ' + THIRD_ONE + '2026-03-05 120,00 EUR' + THIRD_END_ONE + SUFFIX


def test_a_free_movement_in_the_neighbourhood_gives_the_first_and_the_third_hint_together(world):
    """Beidseitig kontierter Übertrag am 2.3. und eine Zahlung gleicher Höhe ohne Satz zehn Tage später auf A: Sie sperrt das
    Zusammenlegen im Umfeld – beide Sätze bleiben, die Zahlung wird gemeldet (Hinweis 1), der Eingangs-Satz als möglicherweise doppelt
    (Hinweis 3)."""
    _out_booked(world, '2026-03-02', '500.00')
    _in_booked(world, '2026-03-02', '500.00')
    _bank(world, world.a, '2026-03-12', '-500.00')
    message = message_of(handlers.handle_datev_export(world.db, _post()))
    assert message == (
        'Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
        '(Eingänge 0,00 EUR, Ausgänge 500,00 EUR) – 2026-03-12 -500,00 EUR. '
        + THIRD_ONE + '2026-03-02 500,00 EUR' + THIRD_END_ONE + THIRD_TRIGGER.format('2026-03-12', '500,00') + SUFFIX)
    assert len(_data_lines(confirmed_export(world.db, *PERIOD)[0])) == 2


def test_all_three_hints_come_in_one_message_in_this_order_and_one_confirmation_covers_them(world):
    _bank(world, world.a, '2026-03-20', '-9.00')                                           # Hinweis 1
    _out_booked(world, '2026-02-28', '100.00')                                             # Hinweis 2: Ausgang im Februar
    _in_booked(world, '2026-03-02', '100.00')
    _late_transfer(world, '2026-03-03', '2026-03-12', '250.00')                            # Hinweis 3
    _expense(world, '2026-03-15')
    before = _date_taxes(world.db)
    result = handlers.handle_datev_export(world.db, _post())
    assert message_of(result) == (
        'Nicht exportiert: 1 Bankbewegung im Zeitraum hat keinen Buchungssatz und steht nicht im Stapel '
        '(Eingänge 0,00 EUR, Ausgänge 9,00 EUR) – 2026-03-20 -9,00 EUR. 1 Übertrag zwischen eigenen Konten steht mit seiner '
        'Ausgangsseite außerhalb des Zeitraums und deshalb nicht in diesem Stapel – 2026-03-02 100,00 EUR. Er gehört in den '
        'Stapel des Zeitraums seiner Ausgangsseite. ' + THIRD_ONE + '2026-03-12 250,00 EUR' + THIRD_END_ONE + SUFFIX)
    assert _date_taxes(world.db) == before
    assert len(_data_lines(confirmed_export(world.db, *PERIOD)[0])) == 3                  # Aufwand, Ausgang und Eingang des späten Übertrags


def test_a_change_of_the_third_hint_between_hint_and_confirmation_asks_again(world, no_export):
    _late_transfer(world)
    old_check = check_of(handlers.handle_datev_export(world.db, _post()))
    _late_transfer(world, '2026-03-20', '2026-03-29', '77.00')
    result = handlers.handle_datev_export(world.db, _post(confirm_export='1', confirmed_check=old_check))
    assert is_confirm_redirect(result) and check_of(result) != old_check
    assert message_of(result).startswith('Nicht exportiert: 2 Überträge zwischen eigenen Konten stehen ')
    assert no_export == [] and not [tax for _id, tax in _date_taxes(world.db) if tax]


def test_the_third_hint_confirmed_exports_both_entries_and_stamps_them(world):
    out_bank = _out_booked(world, '2026-03-02')
    in_bank = _in_booked(world, '2026-03-11')
    csv_bytes, _name = confirmed_export(world.db, *PERIOD)
    assert len(_data_lines(csv_bytes)) == 2
    stamped = {booking_id for booking_id, tax in _date_taxes(world.db) if tax}
    assert {row[0] for bank in (out_bank, in_bank) for row in world.db.get_child_bookings_for_bank(bank)} == stamped


def test_the_third_hint_names_neither_text_nor_recipient(world):
    _out_booked(world, '2026-03-02')
    bank = _bank(world, world.b, '2026-03-11', '100.00', recipient_client='GEHEIM-EMPFAENGER', text='GEHEIM-TEXT')
    _entry(world.db, bank, '2026-03-11', '100.00', 1800, 1810)
    location = handlers.handle_datev_export(world.db, _post())[1]
    assert 'GEHEIM' not in location and 'GEHEIM' not in message_of((303, location))


# ═════════════════════════════════════════════════════════════════════════════
# E. Die Bestätigung: Prüfsumme über Zeitraum und Wortlaut
# ═════════════════════════════════════════════════════════════════════════════

def test_with_the_confirmation_for_exactly_this_hint_the_batch_is_exported_and_stamped(world):
    _scenario(world)

    csv_bytes, filename = confirmed_export(world.db, *PERIOD)

    assert filename == 'DATEV_Buchungsstapel_2026_20260301-20260331.csv'
    assert csv_bytes.startswith(b'"EXTF"') and len(_data_lines(csv_bytes)) == 1          # ein Satz, die sieben fehlen
    assert len([tax for _id, tax in _date_taxes(world.db) if tax]) == 1                  # nur der exportierte Satz


def _check_of_entries(unbooked=(), elsewhere=(), elsewhere_out=(), doubtful=(), trigger=None):
    """Ein ``TransferCheck`` aus Einträgen ``(ID, Datum, Betrag€)`` – so, wie ``Database.check_transfers`` sie liefert."""
    return TransferCheck(list(unbooked), set(), list(elsewhere), list(elsewhere_out), list(doubtful), trigger)


ENTRY = (7, '2026-03-05', Decimal('-1.0000'))


def test_the_check_is_the_hash_of_the_period_and_every_entry_of_every_hint(world):
    """Prüfsumme = die ersten 16 Zeichen von SHA-256 über Zeitraum und ALLE Einträge je Hinweisart – Satz- bzw. Bankbewegungs-ID,
    Datum und Betrag in Minor Units – und den Auslöser, nicht über den Wortlaut (der nennt nur die ersten fünf)."""
    bank = _bank(world, world.a, '2026-03-05', '-1.00')
    result = handlers.handle_datev_export(world.db, _post())
    expected = hashlib.sha256(
        f'2026-03-01|2026-03-31|unbooked:{bank},2026-03-05,-10000|elsewhere:|elsewhere_out:|doubtful:|trigger:'.encode()).hexdigest()[:16]
    assert check_of(result) == expected
    assert expected == handlers._datev_check('2026-03-01', '2026-03-31', world.db.check_transfers('2026-03-01', '2026-03-31'))
    full = _check_of_entries(elsewhere=[(3, '2026-03-02', Decimal('100'))], elsewhere_out=[(4, '2026-03-03', Decimal('5.5'))],
                             doubtful=[(5, '2026-03-04', Decimal('2'))], trigger=('2026-03-01', Decimal('7')))
    assert handlers._datev_check('2026-03-01', '2026-03-31', full) == hashlib.sha256(
        ('2026-03-01|2026-03-31|unbooked:|elsewhere:3,2026-03-02,1000000|elsewhere_out:4,2026-03-03,55000|'
         'doubtful:5,2026-03-04,20000|trigger:2026-03-01,70000').encode()).hexdigest()[:16]


def test_the_check_depends_on_the_period_and_on_id_date_and_amount_of_every_entry_of_every_kind():
    check = handlers._datev_check
    first, second = ENTRY, (8, '2026-03-06', Decimal('-2.0000'))
    base = check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[first]))
    other_id, other_day, other_amount = (9, first[1], first[2]), (7, '2026-03-06', first[2]), (7, first[1], Decimal('-1.0001'))
    checks = {
        base,
        check('2026-03-02', '2026-03-31', _check_of_entries(unbooked=[first])),
        check('2026-03-01', '2026-03-30', _check_of_entries(unbooked=[first])),
        check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[other_id])),
        check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[other_day])),
        check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[other_amount])),
        check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[first, second])),
        check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[second, first])),
        check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[first], doubtful=[second])),
        check('2026-03-01', '2026-03-31', _check_of_entries(elsewhere=[first])),                 # derselbe Eintrag in einer anderen Hinweisart
        check('2026-03-01', '2026-03-31', _check_of_entries(elsewhere_out=[first])),
        check('2026-03-01', '2026-03-31', _check_of_entries(doubtful=[first])),
        check('2026-03-01', '2026-03-31', _check_of_entries(doubtful=[first], trigger=('2026-03-01', Decimal('1')))),
        check('2026-03-01', '2026-03-31', _check_of_entries(doubtful=[first], trigger=('2026-03-02', Decimal('1')))),
        check('2026-03-01', '2026-03-31', _check_of_entries(doubtful=[first], trigger=('2026-03-01', Decimal('2')))),
    }
    assert len(checks) == 15
    assert base == check('2026-03-01', '2026-03-31', _check_of_entries(unbooked=[first]))
    assert len(base) == 16 and base == base.lower() and all(char in '0123456789abcdef' for char in base)


def test_a_change_beyond_the_named_entries_asks_again_although_the_wording_stays_the_same(world, no_export):
    """Sieben unkontierte Ausgänge: Der Hinweis nennt fünf und zählt (Anzahl, Summe) – kontiert der Nutzer den siebten und kommt
    dafür eine andere, gleich hohe Bewegung später im Monat dazu, ändert sich der Wortlaut nicht. Die Bestätigung des alten Standes
    gilt trotzdem nicht mehr: Die Prüfsumme umfasst alle Einträge."""
    ids = [_bank(world, world.a, f'2026-03-{number:02d}', '-10.00') for number in range(1, 8)]
    _entry(world.db, _bank(world, world.a, '2026-03-20', '-5.00'), '2026-03-20', '-5.00', 6815, 1800)
    old = handlers.handle_datev_export(world.db, _post())
    _entry(world.db, ids[6], '2026-03-07', '-10.00', 6815, 1800)
    _bank(world, world.a, '2026-03-25', '-10.00')
    new = handlers.handle_datev_export(world.db, _post(confirm_export='1', confirmed_check=check_of(old)))
    assert is_confirm_redirect(new) and message_of(new) == message_of(old)                 # derselbe Wortlaut …
    assert check_of(new) != check_of(old) and no_export == []                              # … aber keine Bestätigung
    assert not [tax for _id, tax in _date_taxes(world.db) if tax]


def test_a_change_of_the_trigger_alone_asks_again(world, no_export):
    """Der Auslöser im Umfeld (Hinweis 3) gehört zur Prüfsumme: Wandert die Bewegung, die das Zusammenlegen sperrt, kommt der Hinweis
    neu – auch wenn kein genannter Satz sich ändert. Hier eine anders kontierte Zahlung gleicher Höhe zwischen Ausgang und Eingang."""
    _late_transfer(world, '2026-03-02', '2026-03-11', '100.00')
    paid = _bank(world, world.a, '2026-03-05', '-100.00')
    _entry(world.db, paid, '2026-03-05', '-100.00', 6815, 1800)
    old = handlers.handle_datev_export(world.db, _post())
    assert message_of(old) == 'Nicht exportiert: ' + THIRD_ONE + '2026-03-11 100,00 EUR' + THIRD_END_ONE + THIRD_TRIGGER.format(
        '2026-03-05', '100,00') + SUFFIX
    _execute(world.db, 'UPDATE Bookings SET DateBooking = ? WHERE ID = ?', ('2026-03-06', paid))
    new = handlers.handle_datev_export(world.db, _post(confirm_export='1', confirmed_check=check_of(old)))
    assert is_confirm_redirect(new) and check_of(new) != check_of(old) and no_export == []
    assert message_of(new) == message_of(old).replace('2026-03-05 100,00 EUR)', '2026-03-06 100,00 EUR)')


def test_the_same_stock_gives_the_same_check_every_time(world):
    _scenario(world)
    first = check_of(handlers.handle_datev_export(world.db, _post()))
    assert first == check_of(handlers.handle_datev_export(world.db, _post()))


@pytest.mark.parametrize('value', ['', '0', 'on', 'true', 'ja', '2', ' 1', '1 '])
def test_only_the_value_1_confirms(world, no_export, value):
    _scenario(world)
    check = check_of(handlers.handle_datev_export(world.db, _post()))
    result = handlers.handle_datev_export(world.db, _post(confirm_export=value, confirmed_check=check))
    assert is_confirm_redirect(result) and check_of(result) == check
    assert no_export == []


@pytest.mark.parametrize('make_check', [
    pytest.param(lambda right: None, id='fehlt'),
    pytest.param(lambda right: '', id='leer'),
    pytest.param(lambda right: '0' * 16, id='falsch'),
    pytest.param(lambda right: right.upper(), id='Grossbuchstaben'),
    pytest.param(lambda right: ' ' + right, id='Leerzeichen-davor'),
    pytest.param(lambda right: right + ' ', id='Leerzeichen-dahinter'),
    pytest.param(lambda right: right[:-1], id='zu-kurz'),
    pytest.param(lambda right: right + '0', id='zu-lang'),
    pytest.param(lambda right: right[:15] + ('0' if right[15] != '0' else '1'), id='ein-Zeichen-anders'),
])
def test_a_missing_or_wrong_check_asks_again_and_exports_nothing(world, no_export, make_check):
    _scenario(world)
    right = check_of(handlers.handle_datev_export(world.db, _post()))
    extra = {} if make_check(right) is None else {'confirmed_check': make_check(right)}
    result = handlers.handle_datev_export(world.db, _post(confirm_export='1', **extra))
    assert is_confirm_redirect(result) and check_of(result) == right
    assert no_export == [] and not [tax for _id, tax in _date_taxes(world.db) if tax]


def test_the_check_of_another_period_does_not_confirm(world, no_export):
    _scenario(world)
    march_check = check_of(handlers.handle_datev_export(world.db, _post()))
    result = handlers.handle_datev_export(world.db, _post(date_to='2026-03-30', confirm_export='1', confirmed_check=march_check))
    assert is_confirm_redirect(result) and check_of(result) != march_check
    assert query_of(result[1])['datev_to'] == ['2026-03-30'] and no_export == []


def test_a_change_of_the_stock_between_hint_and_confirmation_asks_again(world, no_export):
    """Eine weitere Bewegung ohne Satz nach dem Hinweis: Die alte Prüfsumme passt nicht mehr, der Hinweis kommt neu – und nennt die
    neue Bewegung."""
    _scenario(world)
    old_check = check_of(handlers.handle_datev_export(world.db, _post()))
    _bank(world, world.b, '2026-03-20', '-77.00')

    result = handlers.handle_datev_export(world.db, _post(confirm_export='1', confirmed_check=old_check))

    assert is_confirm_redirect(result) and check_of(result) != old_check
    assert message_of(result).startswith('Nicht exportiert: 8 Bankbewegungen')
    assert no_export == [] and not [tax for _id, tax in _date_taxes(world.db) if tax]


def test_a_change_of_the_amount_of_a_listed_movement_between_hint_and_confirmation_asks_again(world, no_export):
    unbooked, _booked = _scenario(world)
    old_check = check_of(handlers.handle_datev_export(world.db, _post()))
    _execute(world.db, 'UPDATE Bookings SET Amount = Amount * 2 WHERE ID = ?', (unbooked[0],))
    result = handlers.handle_datev_export(world.db, _post(confirm_export='1', confirmed_check=old_check))
    assert is_confirm_redirect(result) and check_of(result) != old_check and no_export == []


def test_when_the_hint_has_gone_away_the_export_needs_no_confirmation(world):
    """Der Nutzer kontiert die Bewegung zwischen Hinweis und Bestätigung: Es gibt nichts mehr zu bestätigen, der Export läuft."""
    movement = _bank(world, world.a, '2026-03-05', '-9.00')
    old_check = check_of(handlers.handle_datev_export(world.db, _post()))
    _entry(world.db, movement, '2026-03-05', '-9.00', 6815, 1800)
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post(confirm_export='1', confirmed_check=old_check))
    assert len(_data_lines(csv_bytes)) == 1


def test_without_a_hint_confirmation_fields_are_ignored(world):
    movement = _bank(world, world.a, '2026-03-05', '-9.00')
    _entry(world.db, movement, '2026-03-05', '-9.00', 6815, 1800)
    csv_bytes, _name = handlers.handle_datev_export(world.db, _post(confirm_export='nein', confirmed_check='nicht-gueltig'))
    assert len(_data_lines(csv_bytes)) == 1


@pytest.mark.parametrize('names', [
    pytest.param({'confirm_unbooked': '1', 'confirmed_from': '2026-03-01', 'confirmed_to': '2026-03-31'}, id='Felder-bis-2.3.4-Entwurf'),
    pytest.param({'confirm_unbooked': '1'}, id='nur-das-alte-Kaestchen'),
    pytest.param({'confirm_existing': '1'}, id='das-Kaestchen-des-WISO-Imports'),
])
def test_the_old_confirmation_fields_have_no_effect(world, no_export, names):
    _scenario(world)
    result = handlers.handle_datev_export(world.db, _post(**names))
    assert is_confirm_redirect(result) and no_export == []


def test_unbalanced_splits_come_before_the_hint(world, no_export):
    _scenario(world)
    movement = _bank(world, world.a, '2026-03-20', '-238.00')
    _entry(world.db, movement, '2026-03-20', '-200.00', 6815, 1800)
    result = handlers.handle_datev_export(world.db, _post())
    assert result[1].startswith('/miscellaneous?datev_export=error&msg=')
    assert 'Export abgelehnt' in message_of(result)
    assert no_export == []


def test_the_turn_of_the_year_comes_before_the_hint(world, no_export):
    _scenario(world)
    result = handlers.handle_datev_export(world.db, _post('2025-12-01', '2026-03-31'))
    assert result[1].startswith('/miscellaneous?datev_export=error&msg=')
    assert message_of(result) == handlers.DATEV_YEAR_REFUSED


def test_the_hint_comes_before_the_remark_that_there_are_no_bookings(world):
    """Eine Bankbewegung ohne Satz und sonst nichts im Zeitraum: erst der Hinweis, nach der Bestätigung „keine Buchungen“."""
    _bank(world, world.a, '2026-03-10', '-50.00')
    first = handlers.handle_datev_export(world.db, _post())
    assert is_confirm_redirect(first)
    second = confirmed_export(world.db, *PERIOD)
    assert second[0] == 303 and 'Keine+Buchungen+im+Zeitraum' in second[1]


def test_the_message_of_an_unexpected_error_is_a_fixed_sentence(world, monkeypatch, capsys):
    """Nie der Text der Ausnahme in der Adresse (seit 2.5.5): ein fester Satz, der volle Fehler steht im Server-Log."""
    def boom(*args, **kwargs):
        raise RuntimeError('100 % kaputt & mehr = Ärger #1 +x')
    monkeypatch.setattr(world.db, 'check_transfers', boom)
    result = handlers.handle_datev_export(world.db, _post())
    assert result[0] == 303 and query_of(result[1])['msg'] == [handlers.DATEV_FAILED]
    assert 'kaputt' not in result[1] and 'kaputt' in capsys.readouterr().err


# ═════════════════════════════════════════════════════════════════════════════
# F. Die Seite
# ═════════════════════════════════════════════════════════════════════════════

def _form(page):
    return page.split('action="/datev/export"')[1].split('</form>')[0]


def _confirm_page(world, **more):
    values = {'datev_confirm': True, 'datev_from': '2026-03-01', 'datev_to': '2026-03-31', 'datev_check': CHECK, **more}
    return PageMiscellaneous(world.db, **values)


def test_the_form_asks_for_the_confirmation_of_exactly_the_hint_that_came(world):
    page = _confirm_page(world)
    form = _form(page)
    notice = escape(DATEV_CONFIRM_NOTICE.format(von='2026-03-01', bis='2026-03-31'))
    assert DATEV_CONFIRM_NOTICE == 'Für den Zeitraum {von} bis {bis} gibt es Hinweise zum Stapel (Meldung unten).'
    assert DATEV_CONFIRM_LABEL == 'Trotzdem exportieren'
    assert f'<p class="warnColor">\u26a0\ufe0f {notice}</p>' in form
    assert ('<input type="checkbox" name="confirm_export" value="1" required>'
            f'&nbsp;{DATEV_CONFIRM_LABEL}</label>') in form
    assert 'name="date_from" value="2026-03-01"' in form and 'name="date_to" value="2026-03-31"' in form
    assert f'<input type="hidden" name="confirmed_check" value="{CHECK}">' in form
    assert form.index(notice) < form.index('name="confirm_export"') < form.index('<button type="submit"')
    assert form.index('name="confirmed_check"') < form.index('<button type="submit"')
    assert page.count('name="confirm_export"') == 1 and ' required>' in form


def test_the_old_field_names_are_gone_from_the_page(world):
    page = _confirm_page(world)
    for old in ('confirm_unbooked', 'confirmed_from', 'confirmed_to', 'datev_export=unbooked', "'unbooked'"):
        assert old not in page


def test_the_notice_uses_existing_classes_only(world):
    page = _confirm_page(world)
    assert '<p class="warnColor">' in page
    css_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'buch.css')
    with open(css_path, encoding='utf-8') as css:
        assert '.warnColor' in css.read()


def test_without_the_hint_there_is_no_box_and_the_current_year_is_preset(world):
    page = PageMiscellaneous(world.db)
    year = date.today().year
    assert 'confirm_export' not in page and 'confirmed_check' not in page and DATEV_CONFIRM_LABEL not in page
    assert f'name="date_from" value="{year}-01-01"' in page and f'name="date_to" value="{year}-12-31"' in page


def test_valid_values_alone_do_not_ask_for_a_confirmation(world):
    """Das Kästchen gehört zum Hinweis (``datev_export=confirm``), nicht zu jedem Zeitraum in der Adresse."""
    page = PageMiscellaneous(world.db, datev_confirm=False, datev_from='2026-03-01', datev_to='2026-03-31', datev_check=CHECK)
    assert 'confirm_export' not in page and 'name="date_from" value="2026-03-01"' not in page


@pytest.mark.parametrize('check', [
    pytest.param('<script>alert(1)</script>', id='skript'),
    pytest.param('"><img src=x onerror=alert(1)>', id='attribut-ausbruch'),
    pytest.param('0123456789ABCDEF', id='grossbuchstaben'),
    pytest.param('0123456789abcde', id='zu-kurz'),
    pytest.param('0123456789abcdef0', id='zu-lang'),
    pytest.param('0123456789abcdeg', id='kein-hex'),
    pytest.param('0123456789abcdef\n', id='zeilenumbruch-dahinter'),
    pytest.param('0123 56789abcdef', id='leerzeichen'),
    pytest.param('0123456789abcdef' * 40, id='sehr-lang'),
    pytest.param('', id='leer'),
    pytest.param(None, id='fehlt'),
])
def test_an_invalid_check_gives_no_box_and_nothing_unmasked(world, check):
    page = _confirm_page(world, datev_check=check)
    year = date.today().year
    assert 'confirm_export' not in page and 'confirmed_check' not in page
    assert f'name="date_from" value="{year}-01-01"' in page
    for bad in ('<script>alert(1)</script>', 'onerror', '<img src=x', 'ABCDEF', '0123456789abcdeg'):
        assert bad not in page


@pytest.mark.parametrize('date_from, date_to', [
    pytest.param('<script>alert(1)</script>', '2026-03-31', id='skript-von'),
    pytest.param('2026-03-01', '"><img src=x onerror=alert(1)>', id='skript-bis'),
    pytest.param('2026-13-01', '2026-03-31', id='monat-13'),
    pytest.param('2026-03-01', '2026-02-30', id='datum-gibt-es-nicht'),
    pytest.param('2025-12-01', '2026-03-31', id='ueber-den-jahreswechsel'),
    pytest.param('2026-03-01', None, id='bis-fehlt'),
    pytest.param(None, None, id='beide-fehlen'),
    pytest.param('', '', id='leer'),
    pytest.param('2026-03-01 ', '2026-03-31', id='leerzeichen'),
])
def test_an_invalid_or_foreign_period_gives_no_box_and_nothing_unmasked(world, date_from, date_to):
    page = _confirm_page(world, datev_from=date_from, datev_to=date_to)
    year = date.today().year
    assert 'confirm_export' not in page and 'confirmed_check' not in page
    assert f'name="date_from" value="{year}-01-01"' in page and f'name="date_to" value="{year}-12-31"' in page
    for bad in ('<script>alert(1)</script>', 'onerror', '<img src=x', '2026-13-01', '2026-02-30', CHECK):
        assert bad not in page


def test_the_page_never_writes(world):
    _scenario(world)
    before = _dump(world.db)
    _confirm_page(world)
    assert _dump(world.db) == before


def test_the_page_carries_the_permanent_note_on_transfers_in_the_datev_block(world):
    """Der Export liefert eine Datei – kein Skript sieht seine Antwort. Was er bei Überträgen weglässt, steht deshalb dauerhaft
    im DATEV-Abschnitt, samt dem Hinweis, dass jede Zeile stehen bleibt, wo es nicht sicher zu erkennen ist."""
    page = PageMiscellaneous(world.db)
    block = page.split('<h2>DATEV Export</h2>')[1].split('<h2>Zahlungszuordnungen</h2>')[0]
    assert DATEV_TRANSFER_NOTE == ('Überträge zwischen eigenen Konten, die auf beiden Bankbewegungen kontiert sind, stehen nur '
                                   'einmal im Stapel (Ausgangsseite). Wo das nicht sicher zu erkennen ist, bleibt jede Zeile '
                                   'stehen und der Export weist darauf hin.')
    assert block.count(escape(DATEV_TRANSFER_NOTE)) == 1 and page.count(escape(DATEV_TRANSFER_NOTE)) == 1
    assert block.index(escape(DATEV_TRANSFER_NOTE)) < block.index('<form method="POST" action="/datev/export">')
    assert '<p class="hint">' + escape(DATEV_TRANSFER_NOTE) + '</p>' in block


def test_the_script_shows_the_hint_as_text_in_the_notice_style(world):
    page = PageMiscellaneous(world.db)
    block = page.split('<h2>DATEV Export</h2>')[1].split('<h2>Zahlungszuordnungen</h2>')[0]
    branch = block.split("status === 'confirm'")[1]
    assert "div.textContent = '\u26a0\ufe0f DATEV-Export: ' + miscMsg;" in branch
    assert 'innerHTML' not in block and 'background:#fff3cd' in branch.split('}')[0]


# ═════════════════════════════════════════════════════════════════════════════
# G. Über den echten Server
# ═════════════════════════════════════════════════════════════════════════════

def test_over_http_the_export_asks_once_and_then_delivers_the_file(server_port, auto_login):
    world = _make_world(auto_login.db)
    _scenario(world)
    headers = {'Origin': own_origin(server_port)}
    form = {'date_from': '2026-03-01', 'date_to': '2026-03-31'}
    before = _date_taxes(world.db)

    first = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf, form=form)
    assert first.status == 303
    location = first.headers['Location']
    query = query_of(location)
    assert query['datev_export'] == ['confirm'] and 'GEHEIM' not in location
    check = query['datev_check'][0]
    assert _date_taxes(world.db) == before                                    # nichts exportiert, nichts gestempelt

    dump = _dump(world.db)
    page = get(server_port, location)
    assert page.status == 200
    assert 'name="confirm_export"' in page.text and ' required>' in page.text
    assert f'name="confirmed_check" value="{check}"' in page.text
    assert 'name="date_from" value="2026-03-01"' in page.text and 'name="date_to" value="2026-03-31"' in page.text
    assert _dump(world.db) == dump                                           # das Laden der Seite schreibt nichts

    assert 'name="confirm_export"' not in get(server_port, '/miscellaneous').text

    wrong = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf,
                 form={**form, 'confirm_export': '1', 'confirmed_check': '0' * 16})
    assert wrong.status == 303 and query_of(wrong.headers['Location'])['datev_check'] == [check]

    confirmed = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf,
                     form={**form, 'confirm_export': '1', 'confirmed_check': check})
    assert confirmed.status == 200
    assert confirmed.headers['Content-Disposition'] == \
        'attachment; filename="DATEV_Buchungsstapel_2026_20260301-20260331.csv"'
    assert confirmed.body.decode('cp1252').startswith('"EXTF"') and len(_data_lines(confirmed.body)) == 1


def test_over_http_a_hostile_address_gives_no_box(server_port, auto_login):
    _scenario(_make_world(auto_login.db))
    page = get(server_port, '/miscellaneous?datev_export=confirm&datev_from=2026-03-01&datev_to=2026-03-31'
                            '&datev_check=%3Cscript%3Ealert(1)%3C/script%3E&msg=%3Cb%3Ex%3C/b%3E')
    assert page.status == 200 and 'confirm_export' not in page.text
    assert '<script>alert(1)</script>' not in page.text and '<b>x</b>' not in page.text


def test_over_http_the_transfer_hint_and_its_confirmation_work_the_same_way(server_port, auto_login):
    world = _make_world(auto_login.db)
    _out_booked(world, '2026-03-31', '700.00')
    _in_booked(world, '2026-04-01', '700.00')
    _expense(world, '2026-04-15')
    headers = {'Origin': own_origin(server_port)}
    form = {'date_from': '2026-04-01', 'date_to': '2026-04-30'}

    first = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf, form=form)
    assert first.status == 303 and query_of(first.headers['Location'])['datev_export'] == ['confirm']
    assert '1 Übertrag zwischen eigenen Konten' in query_of(first.headers['Location'])['msg'][0]

    confirmed = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf,
                     form={**form, 'confirm_export': '1', 'confirmed_check': query_of(first.headers['Location'])['datev_check'][0]})
    assert confirmed.status == 200 and len(_data_lines(confirmed.body)) == 1


def test_over_http_the_third_hint_and_its_confirmation_work_the_same_way(server_port, auto_login):
    world = _make_world(auto_login.db)
    _late_transfer(world)
    headers = {'Origin': own_origin(server_port)}
    form = {'date_from': '2026-03-01', 'date_to': '2026-03-31'}

    first = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf, form=form)
    assert first.status == 303 and query_of(first.headers['Location'])['datev_export'] == ['confirm']
    assert 'möglicherweise doppelt im Stapel' in query_of(first.headers['Location'])['msg'][0]
    check = query_of(first.headers['Location'])['datev_check'][0]
    page = get(server_port, first.headers['Location'])
    assert page.status == 200 and f'name="confirmed_check" value="{check}"' in page.text and 'name="confirm_export"' in page.text

    refused = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf, form={**form, 'confirm_export': '1'})
    assert refused.status == 303
    confirmed = post(server_port, '/datev/export', headers=headers, csrf=auto_login.csrf,
                     form={**form, 'confirm_export': '1', 'confirmed_check': check})
    assert confirmed.status == 200 and len(_data_lines(confirmed.body)) == 2


# ═════════════════════════════════════════════════════════════════════════════
# H. Der Export der Datenbank als SQL meldet einen Fehler ohne Fehlertext
# ═════════════════════════════════════════════════════════════════════════════

def test_a_failing_database_export_gives_a_fixed_message_and_not_the_text_of_the_error(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('geheim: C:\\Daten\\buch.db 100 % & Pfad #1')
    monkeypatch.setattr(world.db, 'export_to_sql', boom)
    status, location = handlers.handle_db_export(world.db)
    assert status == 303
    assert location == '/miscellaneous?export=error&msg=' + quote('Export fehlgeschlagen.')
    assert 'geheim' not in location and 'Daten' not in location and '%' not in location.replace('%20', '')
    assert query_of(location)['msg'] == ['Export fehlgeschlagen.']
